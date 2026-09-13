"""Workers: specialised agents that own a slice of the tool catalogue.

A worker executes one planned step (a tool call with arguments) against the user's working memory
and returns a report: the tool result, the findings it draws from it in plain words, and flags the
orchestrator's reflection reads. Workers never talk to each other; they share memory.

    historian   recall_user_history, list_commitments, list_reserved_flows, list_evidence
    forecaster  project_balance, amount_safe_today, earliest_full_payment_date
    planner     list_payment_options, candidate_spending_changes, enumerate_candidate_plans, rank_and_choose
    scorer      spending_score, expense_impact, account_factors
    auditor     check_plan_safety, counterfactual_without_messages, audit_output_row
    explainer   build_decision_packet, write_explanation
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from .memory import UserMemory
from .tools import registry


@dataclass
class Step:
    id: str
    worker: str
    tool: str
    args: dict = field(default_factory=dict)
    why: str = ""
    phase: str = "analyse"          # analyse | deliver
    optional: bool = False           # an optional step that fails does not fail the plan


@dataclass
class WorkerReport:
    step: Step
    result: dict
    findings: list[str] = field(default_factory=list)
    flags: list[str] = field(default_factory=list)     # machine-readable, read by the reflection

    @property
    def ok(self) -> bool:
        return "error" not in self.result


class Worker:
    name = "worker"

    def tools(self) -> list[str]:
        return [t.name for t in registry.for_worker(self.name)]

    def run(self, mem: UserMemory, step: Step) -> WorkerReport:
        if step.tool not in registry:
            return WorkerReport(step, {"error": f"unknown tool {step.tool}"}, flags=["unknown_tool"])
        result = registry.call(step.tool, mem, worker=self.name, **step.args)
        rep = WorkerReport(step, result)
        if rep.ok:
            self.interpret(mem, step, result, rep)
        else:
            rep.flags.append("tool_error")
            rep.findings.append(f"{step.tool} failed: {result['error']}")
        return rep

    def interpret(self, mem: UserMemory, step: Step, r: dict, rep: WorkerReport) -> None:
        """Turn the tool result into findings and flags. Default: the summary line."""
        if r.get("summary"):
            rep.findings.append(r["summary"])


class Historian(Worker):
    name = "historian"

    def interpret(self, mem, step, r, rep):
        super().interpret(mem, step, r, rep)
        if step.tool == "recall_user_history":
            if r["recurring_credits"] == 0:
                rep.flags.append("no_income_forecast")
                rep.findings.append("no recurring income is forecast: the plan must be carried by the current balance")
            if r["monthly_outflow"] > r["monthly_income"] > 0:
                rep.flags.append("structural_deficit")
                rep.findings.append(f"recurring outflow {r['monthly_outflow']:,.2f}/month exceeds income {r['monthly_income']:,.2f}/month")
        if step.tool == "list_evidence":
            if r["uncertainty_flags"]:
                rep.flags.append("evidence_uncertainty")
                rep.findings.extend(r["uncertainty_flags"][:3])
            if r.get("image_backed"):
                rep.flags.append("image_evidence")
            # pull the raw rows behind uncertainty flags and image-filled amounts from disk, on demand, so the
            # evidence (e.g. the blank amount the image supplied) is in the transcript
            ids = sorted({m for n in r["uncertainty_flags"] + r.get("image_backed", []) for m in re.findall(r"event_\d+", n)})
            if ids:
                if True:
                    raw = registry.call("fetch_events", mem, worker=self.name, event_ids=ids[:5])
                    if "error" not in raw:
                        for row in raw["rows"][:3]:
                            rep.findings.append(f"raw {row['event_id']}: {row['event_date']} {row['event_type']}/{row['category']} "
                                                f"{row['direction']} {row['amount'] or '<blank>'} {row['currency']} [{row['status']}] {row['description'][:60]}")
            if r["facts"]:
                rep.flags.append("message_facts_applied")


class Forecaster(Worker):
    name = "forecaster"

    def interpret(self, mem, step, r, rep):
        super().interpret(mem, step, r, rep)
        if step.tool == "project_balance" and r.get("first_breach"):
            rep.flags.append("baseline_breach")
            rep.findings.append(f"even without the request the balance falls under the minimum on {r['first_breach']}")
        if step.tool == "amount_safe_today" and r["amount_safe_to_pay"] <= 0:
            rep.flags.append("nothing_safe_today")
        if step.tool == "earliest_full_payment_date" and r["earliest_date_for_full_payment"] is None:
            rep.flags.append("no_full_payment_date")


class Planner(Worker):
    name = "planner"

    def interpret(self, mem, step, r, rep):
        super().interpret(mem, step, r, rep)
        if step.tool == "list_payment_options":
            for o in r["options"]:
                if not o["eligible"]:
                    rep.findings.append(f"{o['option_id']} ({o['method']}, {o['n']} payment(s)) rejected: {'; '.join(o['rejected_because'])}")
            if not any(o["eligible"] for o in r["options"]):
                rep.flags.append("no_eligible_option")
        if step.tool == "enumerate_candidate_plans" and not r["candidates"]:
            rep.flags.append("no_safe_plan")
        if step.tool == "rank_and_choose":
            ch = r["chosen"]
            if ch is None:
                rep.flags.append("not_affordable")
            else:
                if not ch["completes_by_deadline"]:
                    rep.flags.append("misses_deadline")
                    rep.findings.append("the best safe plan completes after the desired completion date")
                if ch["changes"]:
                    rep.flags.append("needs_spending_changes")
                    rep.findings.append("spending changes are required: " + ", ".join(ch["changes"]))
                for q in r["rejected"][:2]:
                    rep.findings.append(f"rejected {q['method']}{' ' + q['option_id'] if q['option_id'] else ''}: total {q['total_paid']:,.2f}, "
                                        f"{q['n_payments']} payment(s){', with changes' if q['changes'] else ''}"
                                        f"{'' if q['completes_by_deadline'] else ', misses deadline'}")


class Scorer(Worker):
    name = "scorer"

    def interpret(self, mem, step, r, rep):
        super().interpret(mem, step, r, rep)
        if step.tool == "expense_impact":
            if r["band"] == "unsafe":
                rep.flags.append("impact_unsafe")
            elif r["band"] == "caution":
                rep.flags.append("impact_caution")
                rep.findings.append(f"the request consumes {r['hurt']:.0f}% of the projected headroom")
        if step.tool == "account_factors" and r.get("stability_band") == "fragile":
            rep.flags.append("fragile_account")


class Auditor(Worker):
    name = "auditor"

    def interpret(self, mem, step, r, rep):
        super().interpret(mem, step, r, rep)
        if step.tool == "check_plan_safety" and not r["safe"]:
            rep.flags.append("chosen_plan_unsafe")
        if step.tool == "counterfactual_without_messages" and r.get("depends_on_messages"):
            rep.flags.append("depends_on_messages")
        if step.tool == "audit_output_row" and r["errors"]:
            rep.flags.append("contract_violation")
            rep.findings.extend(r["errors"][:3])


class Explainer(Worker):
    name = "explainer"

    def interpret(self, mem, step, r, rep):
        super().interpret(mem, step, r, rep)
        if step.tool == "write_explanation" and r["usage"].get("fallback"):
            rep.flags.append("template_explanation")


WORKERS: dict[str, Worker] = {w.name: w for w in (Historian(), Forecaster(), Planner(), Scorer(), Auditor(), Explainer())}
