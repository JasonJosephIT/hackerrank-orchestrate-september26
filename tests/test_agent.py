"""Agentic runtime (D13): tool catalogue, planner validation, orchestrator parity with the linear pipeline,
reflection re-planning on an audit failure. No network: explanations use the template."""
import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("bw_main", ROOT / "code" / "main.py")
M = importlib.util.module_from_spec(spec)
spec.loader.exec_module(M)

from buyorwait.agent.memory import LongTermMemory, UserMemory  # noqa: E402
from buyorwait.agent.orchestrator import DELIVER, MANDATORY, PREREQS, Goal, Orchestrator, RulePlanner, validate_plan  # noqa: E402
from buyorwait.agent.tools import registry  # noqa: E402
from buyorwait.agent.workers import WORKERS  # noqa: E402
from buyorwait.evidence import image_amounts  # noqa: E402
from buyorwait.intake import Dataset  # noqa: E402
from buyorwait.plans import request_from_row  # noqa: E402
from buyorwait.verify import load_context  # noqa: E402


@pytest.fixture(scope="module")
def lt():
    ds = Dataset.load(ROOT / "dataset")
    return LongTermMemory(ds, image_amounts(), verify_context=load_context(ROOT / "dataset", "sample_requests.csv"))


def test_tool_catalogue_is_complete_and_owned():
    names = set(registry.names())
    assert set(MANDATORY) <= names and set(DELIVER) <= names
    for n, pres in PREREQS.items():
        assert n in names and all(p in names for p in pres)
    owners = {t.owner for t in registry._tools.values()}
    assert owners == set(WORKERS)
    for s in registry.schemas():                       # OpenAI-style function schemas for tool-calling models
        assert s["type"] == "function" and s["function"]["parameters"]["type"] == "object"


def test_memory_recall_is_per_user_as_of_request_date(lt):
    row = next(lt.ds.samples.itertuples(index=False))
    req = request_from_row(row)
    mem = UserMemory(lt, req)
    st = mem.recall()
    assert st.user_id == req.user_id and st.request_date == req.request_date
    assert set(st.events.user_id) == {req.user_id}                         # nothing from other users
    assert all(r.next_date >= req.request_date for r in st.recurring)     # history projected forward from the request date


def test_validate_plan_inserts_prerequisites_and_mandatory_steps(lt):
    row = next(lt.ds.samples.itertuples(index=False))
    req = request_from_row(row)
    mem = UserMemory(lt, req)
    goal = Goal.from_request(req, mem.profile)
    proposed = [{"tool": "rank_and_choose"}, {"tool": "not_a_tool"}, {"tool": "write_explanation"},
                {"tool": "list_evidence", "args": {"junk": 1}}]
    steps = validate_plan(proposed, goal, mem)
    tools = [s.tool for s in steps]
    assert "not_a_tool" not in tools
    assert tools[-3:] == DELIVER
    seen = set()
    for t in tools:                                    # every prerequisite precedes its dependant
        assert all(p in seen for p in PREREQS.get(t, [])), t
        seen.add(t)
    assert all(m in tools for m in MANDATORY)


def test_orchestrator_matches_linear_pipeline_on_samples(lt):
    orch = Orchestrator(lt, use_llm=False)
    tracer = M.telemetry._NoopTracer()
    for row in lt.ds.samples.itertuples(index=False):
        dec_p, row_p, packet_p, _ = M.run_one_pipeline(lt.ds, lt.image_facts, row, False, tracer)
        res = orch.handle_row(row)
        assert res.row == row_p, row.request_id                             # every column, explanation included
        assert res.reflections[-1].ok and res.reflections[-1].confidence in ("high", "medium", "low")
        assert all(res.reflections[-1].checks[k] for k in ("minimum_respected", "method_accepted", "safe_amount_in_bounds"))
        assert [s.tool for s in res.plan][:1] == ["recall_user_history"]
        assert not [r for r in res.reports if not r.ok and not r.step.optional]


def test_reflection_replans_when_audit_rejects_the_chosen_plan(lt, monkeypatch):
    """Force the auditor to call the first choice unsafe: the orchestrator must re-rank without it."""
    orch = Orchestrator(lt, use_llm=False)
    row = next(r for r in lt.ds.samples.itertuples(index=False) if r.request_id == "request_12")   # installments via option_33
    real = registry.get("check_plan_safety").fn
    calls = {"n": 0}

    def flaky(mem, payments, changes=None):
        calls["n"] += 1
        out = real(mem, payments, changes)
        if calls["n"] == 1:
            out = {**out, "safe": False, "summary": "UNSAFE (injected)"}
        return out
    monkeypatch.setattr(registry.get("check_plan_safety"), "fn", flaky)
    res = orch.handle_row(row)
    assert len(res.reflections) == 2
    assert not res.reflections[0].ok and [s.tool for s in res.reflections[0].replan][0] == "rank_and_choose"
    assert res.reflections[1].ok and res.reflections[1].checks["minimum_respected"]
    assert res.decision.plan is None or res.decision.plan.option_id != "payment_option_33"
    assert any(e["tool"] == "rank_and_choose" and e["args"].get("exclude") for e in res.transcript)


def test_rule_planner_conditions_on_request(lt):
    rows = {r.request_id: r for r in lt.ds.samples.itertuples(index=False)}
    req = request_from_row(rows["request_12"])
    mem = UserMemory(lt, req)
    steps = RulePlanner().plan(Goal.from_request(req, mem.profile), mem)
    assert [s.phase for s in steps][-3:] == ["deliver"] * 3
    assert {s.worker for s in steps} == set(WORKERS)


# ---- D14: account cards and the score gate ---------------------------------------------------
def test_cards_round_trip_and_serve_without_dataset(lt, tmp_path):
    from buyorwait.agent.cards import CardStore
    rows = list(lt.ds.samples.itertuples(index=False))
    store = CardStore.build(lt.ds, lt.image_facts, rows)
    path = tmp_path / "cards.jsonl"
    store.save(path)
    loaded = CardStore.load(path)
    assert len(loaded) == len(rows) and loaded.stats()["avg_streams"] > 0
    card_lt = LongTermMemory(None, lt.image_facts, verify_context=lt.verify_context, cards=loaded)   # dataset dropped
    orch_cards, orch_ds = Orchestrator(card_lt, use_llm=False), Orchestrator(lt, use_llm=False)
    for row in rows:
        a, b = orch_cards.handle_row(row), orch_ds.handle_row(row)
        assert a.row == b.row, row.request_id
        assert a.recall_source == "card" and b.recall_source == "dataset"
        assert a.gate["route"] == b.gate["route"]


def test_score_gate_is_exact_against_full_plan_search(lt):
    """Whenever the gate settles a request, the full enumeration + ranking reaches the same status and plan."""
    from buyorwait.plans import decide
    orch = Orchestrator(lt, use_llm=False)
    gated = 0
    rows = list(lt.ds.samples.itertuples(index=False)) + list(lt.ds.requests.itertuples(index=False))[:60]
    for row in rows:
        res = orch.handle_row(row)
        req = request_from_row(row)
        full = decide(lt.ds, UserMemory(lt, req).recall(), req)
        assert res.decision.status == full.status and res.decision.method == full.method, row.request_id
        assert (res.decision.plan.payments if res.decision.plan else None) == (full.plan.payments if full.plan else None)
        if res.gate["route"] != "search":
            gated += 1
            assert res.gate["route"] == full.status
            assert any(e["summary"].startswith("skipped") for e in res.transcript)
    assert gated > 0
