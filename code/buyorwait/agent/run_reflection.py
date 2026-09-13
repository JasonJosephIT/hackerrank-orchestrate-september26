"""Run-level reflection over telemetry (D19): read the trace file and the agent transcripts of a full run and
summarise what the orchestrator did across every request, so the observability turns into something that can be
read back and acted on.

    summary = summarise(traces_path, transcripts_path)      # dict
    write_report(traces_path, transcripts_path, out_md)     # markdown next to the usage report

Sources
    traces.jsonl             one JSON span per line: buyorwait.request (root), agent.plan, agent.step (worker, tool,
                             iteration, ok, flags), agent.reflect (ok, confidence, concerns, check.*), agent.critique,
                             explain (gen_ai.*, fallback)
    agent_transcripts.jsonl  one AgentResult.summary() per request: goal, planner, gate, findings (flags), reflections, ledger
"""
from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path


def _read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    out = []
    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    return out


def summarise(traces_path: Path, transcripts_path: Path) -> dict:
    spans = _read_jsonl(traces_path)
    transcripts = _read_jsonl(transcripts_path)
    roots = [s for s in spans if s["name"] == "buyorwait.request"]
    steps = [s for s in spans if s["name"] == "agent.step"]
    reflects = [s for s in spans if s["name"] == "agent.reflect"]
    explains = [s for s in spans if s["name"] == "explain"]
    critiques = [s for s in spans if s["name"] == "agent.critique"]
    plans = [s for s in spans if s["name"] == "agent.plan"]

    a = lambda s, k, d=None: s.get("attributes", {}).get(k, d)
    n = len(roots) or len(transcripts)
    by_status = Counter(a(s, "final_status") for s in roots)
    by_conf = Counter(a(s, "confidence") for s in roots)
    status_conf = Counter((a(s, "final_status"), a(s, "confidence")) for s in roots)
    gates = Counter(a(s, "gate") for s in roots)
    recall = Counter(str(a(s, "recall_source")) for s in roots)
    iterations = Counter(int(a(s, "iterations", 1)) for s in roots)
    replanned = sum(1 for s in roots if int(a(s, "iterations", 1)) > 1)
    tool_calls = [int(a(s, "tool_calls", 0)) for s in roots]
    durations = [s.get("duration_ms", 0.0) for s in roots]

    # reflection checks and concerns
    check_fail = Counter()
    concerns = Counter()
    prior_fed = 0
    for s in reflects:
        for k, v in s.get("attributes", {}).items():
            if k.startswith("check.") and v is False:
                check_fail[k[6:]] += 1
        if a(s, "concerns"):
            for c in str(a(s, "concerns")).split(" | "):
                c = re.sub(r"\d{4}-\d{2}-\d{2}", "<date>", c.split(" (")[0])
                c = re.sub(r"request_\d+", "<request>", c)
                concerns[c[:80]] += 1
        if int(a(s, "prior_requests", 0)) > 0 and "earlier request in this run" in str(a(s, "concerns", "")):
            prior_fed += 1

    # worker flags and timings
    flag_counts = Counter()
    for s in steps:
        for f in str(a(s, "flags") or "").split(","):
            if f:
                flag_counts[f] += 1
    tool_ms = defaultdict(list)
    tool_fail = Counter()
    for s in steps:
        tool_ms[a(s, "tool")].append(s.get("duration_ms", 0.0))
        if a(s, "ok") is False:
            tool_fail[a(s, "tool")] += 1
    tool_avg = {t: (sum(v) / len(v), len(v)) for t, v in tool_ms.items() if v}
    skipped = Counter()
    fetches = 0
    for t in transcripts:
        for e in t.get("ledger", []):
            if str(e.get("summary", "")).startswith("skipped"):
                skipped[e["tool"]] += 1
            if e.get("tool") == "fetch_events":
                fetches += 1

    model_calls = sum(1 for s in explains if a(s, "gen_ai.request.model"))
    fallbacks = sum(1 for s in explains if a(s, "buyorwait.fallback_used")) + (len(explains) - model_calls)
    tokens = sum(int(a(s, "gen_ai.usage.input_tokens") or 0) + int(a(s, "gen_ai.usage.output_tokens") or 0)
                 for s in explains + critiques + plans)

    low = []
    for t in transcripts:
        r = (t.get("reflections") or [{}])[-1]
        if r.get("confidence") == "low":
            low.append(dict(request_id=t["request_id"], status=t["row"].get("affordability_status"),
                            method=t["row"].get("recommended_payment_method"), concerns=r.get("concerns", [])[:3]))
    replans = [dict(request_id=t["request_id"], reflections=[x.get("replan") for x in t.get("reflections", [])])
               for t in transcripts if len(t.get("reflections", [])) > 1]

    return dict(
        generated_at=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        traces=str(traces_path), transcripts=str(transcripts_path), requests=n,
        by_status=dict(by_status), by_confidence=dict(by_conf), status_confidence=[(k[0], k[1], v) for k, v in sorted(status_conf.items(), key=lambda kv: (str(kv[0][0]), str(kv[0][1])))],
        gates=dict(gates), recall=dict(recall), iterations=dict(iterations), replanned=replanned,
        tool_calls_avg=(sum(tool_calls) / len(tool_calls)) if tool_calls else 0.0,
        duration_ms_avg=(sum(durations) / len(durations)) if durations else 0.0,
        check_failures=dict(check_fail), concerns=concerns.most_common(12), flags=flag_counts.most_common(12),
        tool_avg_ms=sorted(tool_avg.items(), key=lambda kv: -kv[1][0])[:10], tool_failures=dict(tool_fail),
        skipped=dict(skipped), fetches=fetches, explain_calls=model_calls, template_fallbacks=fallbacks,
        critiques=len(critiques), llm_plans=sum(1 for s in plans if a(s, "gen_ai.request.model")),
        tokens=tokens, prior_feedback=prior_fed, low_confidence=low[:20], replans=replans[:20],
    )


def render(sm: dict) -> str:
    n = max(sm["requests"], 1)
    pct = lambda x: f"{100.0 * x / n:.0f}%"
    L = ["# Run Reflection", "",
         f"Generated {sm['generated_at']} from `{Path(sm['traces']).name}` and `{Path(sm['transcripts']).name}`: "
         f"{sm['requests']} requests served by the orchestrator (D15–D19). The per-request reflection reads worker flags; "
         f"this report reads the telemetry of the whole run so the same signals can be judged across requests.", ""]
    if sm["requests"] == 0:
        L += ["> No agent traces found. Run `python3 code/main.py` (a full run) first.", ""]
        return "\n".join(L)
    L += ["## Outcomes", "", "| Status | Requests | high | medium | low |", "|---|---|---|---|---|"]
    sc = {(s, c): v for s, c, v in sm["status_confidence"]}
    for st, cnt in sorted(sm["by_status"].items(), key=lambda kv: str(kv[0])):
        L.append(f"| {st} | {cnt} ({pct(cnt)}) | {sc.get((st, 'high'), 0)} | {sc.get((st, 'medium'), 0)} | {sc.get((st, 'low'), 0)} |")
    L += ["", f"- Confidence overall: " + ", ".join(f"{k} {v} ({pct(v)})" for k, v in sorted(sm["by_confidence"].items(), key=lambda kv: str(kv[0]))),
          f"- Score gate: " + ", ".join(f"{k} {v} ({pct(v)})" for k, v in sorted(sm["gates"].items(), key=lambda kv: str(kv[0])))
          + (f"; plan-search steps skipped: {sum(sm['skipped'].values())}" if sm["skipped"] else ""),
          f"- Recall source: " + ", ".join(f"{k} {v}" for k, v in sm["recall"].items()),
          f"- Iterations: " + ", ".join(f"{k}× {v}" for k, v in sorted(sm["iterations"].items())) + f"; re-planned requests: {sm['replanned']}",
          f"- Tool calls per request: {sm['tool_calls_avg']:.1f}; wall time per request: {sm['duration_ms_avg']:.0f} ms",
          f"- Raw rows fetched from disk (historian): {sm['fetches']} request(s)",
          f"- Explanations: {sm['explain_calls']} model call(s), {sm['template_fallbacks']} from the template"
          + (" (a --no-llm run)" if sm['explain_calls'] == 0 else "") + f"; critiques {sm['critiques']}, model-planned {sm['llm_plans']}; tokens {sm['tokens']:,}",
          f"- Cross-request feedback applied (earlier request of the same user re-planned / low confidence): {sm['prior_feedback']}", ""]
    L += ["## Reflection checks that failed", ""]
    if sm["check_failures"]:
        L += ["| Check | Requests |", "|---|---|"] + [f"| {k} | {v} |" for k, v in sorted(sm["check_failures"].items(), key=lambda kv: -kv[1])]
    else:
        L.append("None: every request passed all seven goal checks on its final iteration.")
    L += ["", "## Concerns raised by the reflection", ""]
    if sm["concerns"]:
        L += ["| Concern | Requests |", "|---|---|"] + [f"| {c} | {v} ({pct(v)}) |" for c, v in sm["concerns"]]
    else:
        L.append("None.")
    L += ["", "## Worker flags", ""]
    if sm["flags"]:
        L += ["| Flag | Requests |", "|---|---|"] + [f"| {f} | {v} ({pct(v)}) |" for f, v in sm["flags"]]
    L += ["", "## Where the time goes", "", "| Tool | Avg ms | Calls |", "|---|---|---|"]
    L += [f"| {t} | {ms:.1f} | {cnt} |" for t, (ms, cnt) in sm["tool_avg_ms"]]
    if sm["tool_failures"]:
        L += ["", "Tool errors: " + ", ".join(f"{k} ×{v}" for k, v in sm["tool_failures"].items())]
    if sm["replans"]:
        L += ["", "## Re-planned requests", ""] + [f"- {r['request_id']}: {r['reflections']}" for r in sm["replans"]]
    L += ["", "## Low-confidence requests", ""]
    if sm["low_confidence"]:
        L += ["| Request | Status / method | Concerns |", "|---|---|---|"]
        L += [f"| {r['request_id']} | {r['status']} / {r['method']} | {'; '.join(r['concerns'])} |" for r in sm["low_confidence"]]
    else:
        L.append("None.")
    L += ["", "## Reading this", "",
          "- A concern that appears on most requests is a property of the dataset, not of those users (e.g. thin headroom when requests are sized near the limit).",
          "- `completes_by_deadline` fails whenever no safe plan meets the desired date (a `not_affordable`, or a `wait` past it); it never triggers a re-plan. Any other failed check on the final iteration means the re-plan budget ran out: inspect the request's transcript.",
          "- Low confidence never changes a contract column; it changes what the explanation says and what a reviewer looks at first.", ""]
    return "\n".join(L)


def write_report(traces_path: Path, transcripts_path: Path, out_md: Path) -> Path:
    out_md.parent.mkdir(parents=True, exist_ok=True)
    out_md.write_text(render(summarise(traces_path, transcripts_path)), encoding="utf-8")
    return out_md
