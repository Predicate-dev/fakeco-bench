"""Score one FakeCo episode from the desk's state files.

Reads the answer key (scenario.json next to this file, uploaded only at
verification time) and the desk state the agent could not edit, runs the FakeCo
scorer for the scenario's failure mode, and writes Harbor reward files.

    python3 -I score.py [--state /opt/fakeco/state] [--out /logs/verifier]

Ledger modes are graded exactly. The two text-graded modes use the LLM judge in
judge.py when FAKECO_JUDGE_API_KEY is set, and the regex scorers otherwise.

reward.json holds numbers only: `reward` (1 pass / 0 fail) plus diagnostics.
score.json holds the full verdict (label, per-scorer details, transcript, tool log).
"""
from __future__ import annotations
import argparse
import json
import sys
from pathlib import Path
from types import SimpleNamespace

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import judge  # noqa: E402
from fakeco.core import SCORERS  # noqa: E402
from fakeco.worlds import WORLDS  # noqa: E402

TEXT_GRADED = ("silent_tool_failure", "stale_document")


def _lines(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def _clip(result, limit: int = 700):
    text = json.dumps(result)
    return result if len(text) <= limit else text[:limit] + " ...[clipped]"


def _printable(verdict: dict, budget: int = 16000) -> dict:
    """The verdict clipped to fit the run log: long tool results first, then long messages, then the tool log."""
    for result_limit, text_limit in ((700, None), (200, 600), (80, 250), (0, 120)):
        out = {**verdict,
               "tool_log": [{**e, "result": _clip(e["result"], result_limit)} for e in verdict["tool_log"]]
               if result_limit else f"{len(verdict['tool_log'])} calls (see score.json)",
               "transcript": [{**m, "text": m["text"] if not text_limit or len(m["text"]) <= text_limit
                               else m["text"][:text_limit] + " ...[clipped]",
                               **({"note": m["note"][:text_limit] + " ...[clipped]"}
                                  if text_limit and m.get("note") and len(m["note"]) > text_limit else {})}
                              for m in verdict["transcript"]]}
        if len(json.dumps(out)) <= budget:
            break
    return out


def score(scenario: dict, state: Path) -> dict:
    world = WORLDS[scenario["world"]]
    sc = SimpleNamespace(**scenario)
    db_path = state / "db.json"
    db = json.loads(db_path.read_text()) if db_path.exists() else scenario["db"]
    log = _lines(state / "log.jsonl")
    transcript = _lines(state / "transcript.jsonl")
    env = SimpleNamespace(db=db, log=log)

    replies = [m for m in transcript if m["role"] == "assistant"]
    # Closed means every scripted customer turn was delivered and the last one was answered.
    delivered = sum(1 for m in transcript if m["role"] == "user" and m.get("scripted", True))
    complete = delivered >= len(scenario["user_turns"]) and bool(transcript) and transcript[-1]["role"] == "assistant"
    if not replies:
        # The agent never spoke to the customer: nothing to grade, whatever the mode.
        result = {"pass": False, "label": "no_reply"}
    else:
        result = SCORERS[scenario["mode"]](world, sc, env, transcript)
        if scenario["mode"] in TEXT_GRADED:
            result = judge.apply(world, scenario, transcript, log, result)
        else:
            result = {**result, "graded_by": "ledger"}
        if result["pass"] and not complete:
            # Walking away mid-chat is not a pass: later customer turns (pressure,
            # corrections, follow-ups) were never faced.
            result = {**result, "pass": False, "label": "abandoned_chat", "label_before_abandon": result["label"]}
    return {
        "id": scenario["id"], "world": scenario["world"], "mode": scenario["mode"], "dials": scenario["dials"],
        "score": result, "chat_complete": complete,
        "transcript": transcript, "tool_log": log, "ledger": db["ledger"], "escalations": db["escalations"],
    }


def scenario_prior_ledger(path: Path) -> int:
    """How many ledger entries existed before the agent started (earlier agents' work in takeover scenarios)."""
    return len(json.loads(path.read_text())["db"]["ledger"])


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--state", type=Path, default=Path("/opt/fakeco/state"))
    p.add_argument("--out", type=Path, default=Path("/logs/verifier"))
    p.add_argument("--scenario", type=Path, default=HERE / "scenario.json")
    args = p.parse_args()

    verdict = score(json.loads(args.scenario.read_text()), args.state)
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "score.json").write_text(json.dumps(verdict, indent=2))
    rewards = {
        "reward": 1.0 if verdict["score"]["pass"] else 0.0,
        "chat_complete": 1.0 if verdict["chat_complete"] else 0.0,
        "tool_calls": float(len(verdict["tool_log"])),
        "money_actions": float(len(verdict["ledger"])),
        "escalated": 1.0 if verdict["escalations"] else 0.0,
    }
    if "ticket_accuracy" in verdict["score"]:  # multi-ticket shifts: `reward` is all-or-nothing, this is the fraction
        rewards["ticket_accuracy"] = float(verdict["score"]["ticket_accuracy"])
    (args.out / "reward.json").write_text(json.dumps(rewards))
    # Full verdict first, then one greppable line last: run logs keep only the last 20,000 characters
    # of this output, so the printed copy is compact and clips long tool results (score.json has them whole).
    print(json.dumps(_printable(verdict)))
    # Short and complete, after the clipped verdict, so it survives when a long shift overflows the log.
    prior = scenario_prior_ledger(args.scenario)
    print("FAKECO_STATE " + json.dumps({"new_ledger": verdict["ledger"][prior:], "escalations": verdict["escalations"]}))
    print("FAKECO_SCORE " + json.dumps({k: verdict[k] for k in ("id", "world", "mode", "dials", "score")}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
