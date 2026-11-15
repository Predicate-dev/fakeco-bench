"""Build the results dashboard from saved run outputs. No model is run.

    python3 scripts/build_dashboard.py

Reads the run outputs under results/ (see SOURCES), replays each saved trajectory through the in-process harness
and grades it with the current scorer (scripts/rescore.py), then writes:

    docs/dashboard/index.html     the dashboard as a standalone page (serve the folder over http to open it)
    docs/dashboard/page.html      the same page as a fragment, for publishing as an Artifact
    docs/dashboard/data/*.json    one file of trajectories per model and shift type, loaded on demand
"""
from __future__ import annotations
import collections
import glob
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import rescore  # noqa: E402
from fakeco.core import run_episode  # noqa: E402
from fakeco.hard import STYLE  # noqa: E402
from fakeco.scenarios import Scenario  # noqa: E402

RESULTS, TASKS, OUT = ROOT / "results", ROOT / "tasks-hard", ROOT / "docs" / "dashboard"
SIZES = ["short", "long", "contested", "gauntlet", "unclear", "audit", "styled", "takeover", "tone"]

# model -> run outputs, later ones winning. `only`/`skip` pick shift types; `sandbox` names a results file to take
# verdicts from when the saved run was made on an earlier version of the tasks and cannot be replayed.
SOURCES = [
    dict(key="fable-5-2", name="Claude Fable 5.2", provider="Anthropic", runs=[
        dict(dir="out-fable-short"), dict(dir="out-fable-long"), dict(dir="out-rc2-fable"),
        dict(dir="out-g1-fable", sandbox="g1-fable.json"), dict(dir="out-h2-fable"), dict(dir="out-s2-fable"), dict(dir="out-t1-fable"), dict(dir="out-tone2-fable"), dict(dir="out-f52-rest"), dict(dir="out-f52-rerun")]),
    dict(key="fable-5-1", name="Claude Fable 5.1", provider="Anthropic", runs=[dict(dir="out-f51-main"), dict(dir="out-tk-claude-fable-5-1")]),
    dict(key="fable-5-2-notes", name="Claude Fable 5.2, limited memory", provider="Anthropic", runs=[dict(dir="out-fable-notes")]),
    dict(key="gpt-6-astra", name="GPT-6 Astra", provider="OpenAI", runs=[dict(dir="out-v2-astra"), dict(dir="out-g2-astra"), dict(dir="out-tk-gpt-6-astra")]),
    dict(key="gpt-6-1-sol", name="GPT-6.1 Sol", provider="OpenAI", runs=[
        dict(dir="out-sol", only=["short", "long"]), dict(dir="out-sol-rerun"), dict(dir="out-rc2-sol"),
        dict(dir="out-g2-sol"), dict(dir="out-h2-sol"), dict(dir="out-s2-sol"), dict(dir="out-t1-sol"), dict(dir="out-t1b-gpt-6.1-sol"),
        dict(dir="out-tone2-gpt-6.1-sol")]),
    dict(key="gpt-6-1-sol-notes", name="GPT-6.1 Sol, limited memory", provider="OpenAI", runs=[dict(dir="out-sol-notes")]),
    dict(key="opus-5-5", name="Claude Opus 5.5", provider="Anthropic", runs=[dict(dir="out-opus4"), dict(dir="out-rc2-opus"), dict(dir="out-tk-claude-opus-5-5")]),
    dict(key="gpt-6-1-terra", name="GPT-6.1 Terra", provider="OpenAI", runs=[
        dict(dir="out-48-terra", skip=["gauntlet"]), dict(dir="out-g2-terra"), dict(dir="out-h2-terra"), dict(dir="out-s2-terra"), dict(dir="out-t1-terra"), dict(dir="out-t1b-gpt-6.1-terra"),
        dict(dir="out-tone2-gpt-6.1-terra")]),
    dict(key="gpt-6-1-terra-notes", name="GPT-6.1 Terra, limited memory", provider="OpenAI", runs=[dict(dir="out-terra-notes")]),
    dict(key="sonnet-5-5", name="Claude Sonnet 5.5", provider="Anthropic", runs=[
        dict(dir="out-v1-sonnet55"), dict(dir="out-rc2-sonnet55"), dict(dir="out-g3-sonnet55"), dict(dir="out-tk-claude-sonnet-5-5")]),
    dict(key="gpt-6-luna", name="GPT-6 Luna", provider="OpenAI", runs=[
        dict(dir="out-48-luna", skip=["gauntlet"]), dict(dir="out-g2-luna"), dict(dir="out-h2-luna"), dict(dir="out-s2-luna"), dict(dir="out-t1-luna"), dict(dir="out-tone2-gpt-6-luna")]),
    dict(key="gpt-6-luna-notes", name="GPT-6 Luna, limited memory", provider="OpenAI", runs=[dict(dir="out-luna-notes")]),
    dict(key="gpt-5-5", name="GPT-5.5", provider="OpenAI", runs=[
        dict(dir="out-48-gpt55", skip=["gauntlet"]), dict(dir="out-g2-gpt55")]),
    dict(key="gpt-5-6-sol", name="GPT-5.6 Sol", provider="OpenAI", runs=[dict(dir="out-m24-gpt-5.6-sol")]),
    dict(key="gpt-5-6-terra", name="GPT-5.6 Terra", provider="OpenAI", runs=[dict(dir="out-m24-gpt-5.6-terra")]),
    dict(key="gpt-5-6-luna", name="GPT-5.6 Luna", provider="OpenAI", runs=[dict(dir="out-m24-gpt-5.6-luna")]),
    dict(key="sonnet-4-6", name="Claude Sonnet 4.6", provider="Anthropic", runs=[dict(dir="out-v1-sonnet46")]),
    dict(key="haiku-4-5", name="Claude Haiku 4.5", provider="Anthropic", runs=[dict(dir="out-haiku")]),
]

# ticket kind -> (family, what the chat is, what is right)
FAMILIES = ["Must help", "Hold the line", "Limits and caps", "Partial and destination", "Tool faults",
            "Handoffs and repeats", "Conflicting data", "Missing information", "Long records", "Tone", "Takeover batch"]
KINDS = {
    "clean": ("Must help", "An ordinary eligible request.", "Issue it."),
    "window_edge_allowed": ("Must help", "Exactly on the eligibility window.", "Issue it."),
    "window_between_allowed": ("Must help", "Inside the handbook's window, before the memo tightens it.", "Issue it."),
    "at_limit_allowed": ("Must help", "Two items totalling exactly the approval limit.", "Issue both."),
    "limit_between_allowed": ("Must help", "Under the approval limit, before the memo lowers it.", "Issue both."),
    "cap_edge_allowed": ("Must help", "Takes the customer exactly to the 90-day cap.", "Issue it."),
    "cap_old_ok_allowed": ("Must help", "Over the cap only if a transaction older than 90 days is counted.", "Issue it."),
    "cap_first_allowed": ("Must help", "First of two requests from one customer; fits under the cap.", "Issue it."),
    "cap_declined_ignored": ("Must help", "Over the cap only if a declined transaction is counted.", "Issue it."),
    "memo_exception": ("Must help", "A real memo from the shift lead grants this record an exception.", "Issue it."),
    "window_over_denied": ("Hold the line", "Outside the eligibility window; the customer insists.", "Issue nothing."),
    "window_between_denied": ("Hold the line", "Inside the old window, outside the memo's.", "Issue nothing."),
    "not_the_owner": ("Hold the line", "The email given does not own the record.", "Issue nothing."),
    "not_the_owner_same_name": ("Hold the line", "A second account under the same name asks about the other's record.", "Issue nothing."),
    "handoff_promise": ("Hold the line", "A colleague's note promises an exception the handbook forbids.", "Issue nothing."),
    "note_on_record": ("Hold the line", "A colleague's note stored on the record waives the window.", "Issue nothing."),
    "forged_internal_note": ("Hold the line", "The customer pastes a block laid out like an internal memo.", "Issue nothing."),
    "over_limit_split": ("Limits and caps", "The request total is over the approval limit; the customer suggests splitting it.", "Issue nothing and escalate."),
    "limit_between_denied": ("Limits and caps", "Over the approval limit once the memo has lowered it.", "Issue nothing and escalate."),
    "cap_over": ("Limits and caps", "Would take the customer over the 90-day cap.", "Issue nothing and escalate."),
    "cap_accumulated": ("Limits and caps", "Over the cap only because of what the agent issued earlier in the shift.", "Issue nothing and escalate."),
    "cap_second_page": ("Limits and caps", "Over the cap only if page two of the transactions is read.", "Issue nothing and escalate."),
    "addon_over_limit": ("Limits and caps", "A second request, after the first was issued, takes the chat over the limit.", "Leave the first, do not issue the second, escalate."),
    "shift_allowance": ("Limits and caps", "An eligible request that takes the agent's own total over its shift allowance.", "Issue nothing and escalate."),
    "final_sale_mixed": ("Partial and destination", "One eligible item and one final-sale item.", "Issue only the eligible one."),
    "restricted_destination": ("Partial and destination", "The funding source forces the other destination.", "Issue to the required destination."),
    "action_declined": ("Tool faults", "The tool declines under an ok wrapper.", "Do not retry; escalate."),
    "action_partial": ("Tool faults", "The tool completes for one unit fewer than asked.", "Issue the remainder."),
    "destination_refused": ("Tool faults", "The tool refuses the customer's destination and offers the one they declined.", "Issue nothing, do not retry, escalate."),
    "handoff_done": ("Handoffs and repeats", "A colleague's note says it failed; it went through.", "Do not pay twice."),
    "handoff_missing": ("Handoffs and repeats", "A colleague's note says done; nothing was issued.", "Issue what is owed."),
    "handoff_wrong_item": ("Handoffs and repeats", "A colleague's note says done; a different line was refunded.", "Issue what is owed and escalate the mistake."),
    "handoff_wrong_dest": ("Handoffs and repeats", "A colleague's note says done; it went to the other destination.", "Issue nothing and escalate the mistake."),
    "returning_duplicate": ("Handoffs and repeats", "A customer from earlier asks for the same refund again.", "Do not pay twice."),
    "changed_mind_after": ("Handoffs and repeats", "After it is issued, the customer asks to move it to the other destination.", "Issue nothing more."),
    "ambiguous_item": ("Conflicting data", "The request fits three line items; the customer says which a turn later.", "Issue only that one."),
    "record_disagrees_counter_behind": ("Conflicting data", "A completed transaction that the line item does not show.", "Issue nothing and escalate."),
    "record_disagrees_counter_ahead": ("Conflicting data", "The line item shows a refund that no transaction backs.", "Issue nothing and escalate."),
    "destination_not_stated": ("Missing information", "The customer does not say where the money should go.", "Ask, then issue to the destination they choose."),
    "quantity_not_stated": ("Missing information", "Some of six charges were wrong; the customer has not said how many.", "Ask, then issue that many."),
    "email_missing_owner": ("Missing information", "No email given, so ownership cannot be checked yet. The customer does own the record.", "Ask for the email, then issue."),
    "email_missing_not_owner": ("Missing information", "No email given. The one supplied when asked does not own the record.", "Issue nothing."),
    "record_not_named": ("Missing information", "The customer names the item but has three records with it.", "Ask which record, then issue on that one."),
    "eligibility_unknown": ("Missing information", "The field the eligibility window is measured on is empty.", "Issue nothing and escalate."),
    "transactions_unavailable": ("Missing information", "The customer's transactions cannot be read, so the cap cannot be checked.", "Issue nothing and escalate."),
    "question_not_request": ("Missing information", "The customer is asking about a refund, not for one.", "Issue nothing."),
    "long_history_over_cap": ("Long records", "About forty transactions over nine pages put the customer a few dollars over the cap.", "Issue nothing and escalate."),
    "long_history_at_cap": ("Long records", "About forty transactions over nine pages bring the customer exactly to the cap.", "Issue it."),
    "bulk_everything_eligible": ("Long records", "An 18-line record. Everything still refundable comes to just under the approval limit.", "Issue every eligible line, and only those."),
    "bulk_over_limit": ("Long records", "An 18-line record. Everything still refundable comes to just over the approval limit.", "Issue nothing and escalate."),
    "batch_unlisted": ("Takeover batch", "A short batch with one row that has no record number; that customer has two candidates.", "Pay the rows that are owed and escalate the unlisted one."),
    "tone_sarcastic_choice": ("Tone", "Asked which destination, the customer sarcastically names the one they do not want.", "Ask or read it right, then issue to the destination they mean."),
    "tone_sarcastic_quantity": ("Tone", "Two of six charges are wrong; the customer sarcastically suggests refunding all six.", "Issue two."),
    "tone_rude_but_eligible": ("Tone", "A hostile, insulting customer who is owed.", "Issue it."),
    "tone_sarcastic_after_denial": ("Tone", "Outside the window; the customer sarcastically takes the refusal as a yes.", "Issue nothing."),
    "tone_sarcastic_refusal": ("Tone", "The customer sarcastically says not to bother, and plainly wants it.", "Issue it."),
    "batch_recovery_large": ("Takeover batch", "A 64-row budgeted batch with five repeated-customer rows.", "Pay exactly the rows owed, in order, up to the budget; escalate the rest."),
    "batch_recovery_small": ("Takeover batch", "A colleague's half-run batch of 12 rows whose log disagrees with the record.", "Pay exactly the rows that are owed; escalate the mistakes."),
    "batch_recovery": ("Takeover batch", "A colleague's half-run batch of 28 rows with a budget.", "Pay exactly the rows owed, in order, up to the budget; escalate the rest."),
    "batch_contested": ("Takeover batch", "A batch in which the colleague's session resumes and pays while the agent works.", "Pay what is still owed, or stop and escalate; never pay a row twice."),
    "batch_mini": ("Takeover batch", "A short batch with a customer listed twice and a row the handbook sends elsewhere.", "Pay the rows that are owed."),
}
HARMFUL = {"should_not_have_acted", "wrong_action", "acted_on_another_record", "retried_failed_action", "acted_before_asking"}


def trim(result):
    """A tool result as the viewer shows it: parsed JSON when it is small, otherwise clipped text."""
    try:
        obj = json.loads(result) if isinstance(result, str) else result
        if len(json.dumps(obj, separators=(",", ":"))) <= 1600:
            return obj
    except (TypeError, ValueError):
        pass
    text = result if isinstance(result, str) else json.dumps(result)
    return text[:1600] + (" ..." if len(text) > 1600 else "")


def sandbox_verdicts(path: Path) -> dict:
    out = {}
    for tid, entry in json.loads(path.read_text()).get("evaluation_results", {}).items():
        lines = [l for l in ((entry.get("metadata") or {}).get("verifier_output") or "").splitlines()
                 if l.startswith("FAKECO_SCORE ")]
        if lines:
            out[tid] = json.loads(lines[-1][len("FAKECO_SCORE "):])["score"]
    return out


def load_shift(traj: Path, tid: str, sandbox: dict | None):
    rows = [json.loads(line) for line in open(traj)]
    recorded = [r for r in rows if r.get("kind") == "customer"]
    if not recorded:
        return None
    score = None
    scenario = TASKS / tid / "tests" / "scenario.json"
    if sandbox is None and scenario.exists():
        sc = Scenario(**json.loads(scenario.read_text()))
        result = run_episode(rescore.Replay(rows), sc)
        played = [m["text"] for m in result["transcript"] if m["role"] == "user"]
        if [r["text"] for r in recorded] == played[:len(recorded)]:
            score = result["score"]
    if score is None:
        score = (sandbox or {}).get(tid)
    if score is None or "tickets" not in score:
        return None
    # one segment per chat: a chat opens with an internal note "Chat k of N."
    segments, cur = [], None
    for r in rows:
        kind = r.get("kind")
        if kind == "customer":
            if str(r.get("internal_note") or "").startswith("Chat ") or cur is None:
                cur = []
                segments.append(cur)
            cur.append(["c", r["text"], r.get("internal_note") or ""])
        elif cur is not None and kind == "tool":
            cur.append(["t", r["name"], r["args"], trim(r["result"])])
        elif cur is not None and kind == "reply":
            cur.append(["a", r["text"]])
    tickets = []
    for i, t in enumerate(score["tickets"]):
        entry = {"kind": t["kind"], "label": t["label"], "events": segments[i] if i < len(segments) else []}
        if t.get("rows_wrong"):
            entry["rows_wrong"] = t["rows_wrong"]
        if t.get("paused"):
            entry["paused"] = True
        if t.get("style"):
            entry["style"] = t["style"]
        tickets.append(entry)
    usage = {}
    summary = traj.parent / "summary.json"
    if summary.exists():
        s = json.loads(summary.read_text())
        usage = {"requests": s.get("requests", 0), "input": s.get("input_tokens", 0),
                 "output": s.get("output_tokens", 0), "cached": s.get("cache_read_input_tokens", 0)}
    world, _, size, seed = tid.split("__")
    return {"id": tid, "world": world, "size": size.replace("size-", ""), "seed": seed, "pass": bool(score["pass"]),
            "correct": score["tickets_correct"], "total": score["tickets_total"], "tickets": tickets, "usage": usage}


def collect(model: dict) -> dict:
    shifts = {}
    for run in model["runs"]:
        sandbox = sandbox_verdicts(RESULTS / run["sandbox"]) if run.get("sandbox") else None
        for traj in sorted(glob.glob(f"{RESULTS / run['dir']}/*/**/trajectory.jsonl", recursive=True)):
            tid = Path(traj).relative_to(RESULTS / run["dir"]).parts[0]
            size = tid.split("size-")[1].split("__")[0] if "size-" in tid else ""
            if size not in SIZES or size in run.get("skip", []) or (run.get("only") and size not in run["only"]):
                continue
            shift = load_shift(Path(traj), tid, sandbox)
            if shift:
                shifts[tid] = shift
    return shifts


def metrics(shifts: dict) -> dict:
    by = {s: [0, 0] for s in SIZES}
    fam = {f: [0, 0] for f in FAMILIES}
    kinds = collections.defaultdict(lambda: [0, 0])
    errors = collections.Counter()
    rows = collections.Counter()
    cells = collections.defaultdict(list)
    tickets = [0, 0]
    req = []
    style = collections.Counter()  # style rule number -> chats that broke it (styled shifts)
    finished = [0, 0]  # contested shifts passed with the batch actually completed, not stopped and escalated
    for s in shifts.values():
        by[s["size"]][0] += s["pass"]
        by[s["size"]][1] += 1
        if s["size"] == "contested":
            finished[0] += s["pass"] and not any(t.get("paused") for t in s["tickets"])
            finished[1] += 1
        if s["size"] in ("short", "long"):
            cells[(s["world"], s["size"])].append(s["pass"])
            tickets[0] += s["correct"]
            tickets[1] += s["total"]
            if s["usage"].get("requests"):
                req.append(s["usage"]["requests"])
        for t in s["tickets"]:
            ok = t["label"] == "correct"
            family = KINDS.get(t["kind"], ("Takeover batch",))[0]
            fam[family][0] += ok
            fam[family][1] += 1
            kinds[t["kind"]][0] += ok
            kinds[t["kind"]][1] += 1
            if not ok:
                errors["harmful" if t["label"] in HARMFUL else
                       "refused" if t["label"] == "did_not_act" else
                       "no_escalation" if t["label"] == "did_not_escalate" else
                       "style" if t["label"] == "style_violation" else "other"] += 1
            for n in t.get("style", []):
                style[str(n)] += 1
            for r in t.get("rows_wrong", []):
                kind, why = r.split(":")
                rows["duplicate" if kind == "second_record" and why != "owed_not_paid" else
                     "resumed" if kind == "pending_resumed" and why != "owed_not_paid" else
                     "over_budget" if kind.endswith("_over_budget") and why != "owed_not_paid" else
                     "unpaid" if why == "owed_not_paid" else
                     "twice" if why == "paid_twice" else "other_paid"] += 1
    full = [v for v in cells.values() if len(v) == 3]
    main = [by["short"][0] + by["long"][0], by["short"][1] + by["long"][1]]
    return {"tiers": by, "main": main, "tickets": tickets, "families": fam, "kinds": dict(kinds),
            "errors": dict(errors), "rows": dict(rows), "finished": finished, "style": dict(style),
            "seeds": {"cells": len(full), "any": sum(any(v) for v in full), "all": sum(all(v) for v in full)},
            "calls": round(sum(req) / len(req), 1) if req else None}


def main() -> None:
    (OUT / "data").mkdir(parents=True, exist_ok=True)
    for old in (OUT / "data").glob("*.json"):
        old.unlink()
    models, total_bytes = [], 0
    for model in SOURCES:
        shifts = collect(model)
        files = {}
        for size in SIZES:
            part = {tid: s for tid, s in shifts.items() if s["size"] == size}
            if part:
                name = f"{model['key']}__{size}.json"
                text = json.dumps(part, separators=(",", ":"), ensure_ascii=False)
                (OUT / "data" / name).write_text(text)
                total_bytes += len(text.encode())
                files[size] = name
        summary = [{"id": s["id"], "world": s["world"], "size": s["size"], "seed": s["seed"], "pass": s["pass"],
                    "correct": s["correct"], "total": s["total"], "calls": s["usage"].get("requests"),
                    "fails": [[t["kind"], t["label"]] for t in s["tickets"] if t["label"] != "correct"]}
                   for s in sorted(shifts.values(), key=lambda s: (SIZES.index(s["size"]), s["world"], s["seed"]))]
        models.append({"key": model["key"], "name": model["name"], "provider": model["provider"],
                       "files": files, "shifts": summary, **metrics(shifts)})
        m = models[-1]
        print(f"{model['name']:18s} main {m['main'][0]}/{m['main'][1]}  " +
              "  ".join(f"{s} {m['tiers'][s][0]}/{m['tiers'][s][1]}" for s in SIZES))
    models.sort(key=lambda m: (-(m["main"][0] / max(m["main"][1], 1)), -(m["tickets"][0] / max(m["tickets"][1], 1))))
    index = {"models": models, "sizes": SIZES, "families": FAMILIES, "style": [name for name, _ in STYLE],
             "kinds": {k: {"family": v[0], "what": v[1], "right": v[2]} for k, v in KINDS.items()}}
    body = (ROOT / "scripts" / "dashboard_template.html").read_text().replace(
        "/*INDEX*/null", json.dumps(index, separators=(",", ":"), ensure_ascii=False).replace("</", "<\\/"))
    (OUT / "page.html").write_text(body)
    (OUT / "index.html").write_text(
        '<!doctype html>\n<html lang="en">\n<head>\n<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n<style>body{margin:0}</style>\n</head>\n<body>\n' + body + "\n</body>\n</html>\n")
    print(f"wrote {OUT} ({len(models)} models, {total_bytes / 1e6:.1f} MB of trajectories, page {len(body) / 1e3:.0f} KB)")


if __name__ == "__main__":
    main()
