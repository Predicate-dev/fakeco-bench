"""Re-score saved runs with the current scorer, without running a model.

    python3 scripts/rescore.py OUTPUT_DIR [--tasks tasks-hard] [--json out.json]

OUTPUT_DIR is what `valkyrie run outputs RUN_ID --output-dir OUTPUT_DIR` wrote. Each task's trajectory holds the
agent's tool calls and replies. The environment is deterministic given those, so replaying them through the
in-process harness rebuilds the same ledger, tool log and chat, which the current scorer then grades.
A replay that stops matching the recording (the agent crashed or was cut off) is graded on what it did.
"""
import argparse
import glob
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from fakeco.core import run_episode  # noqa: E402
from fakeco.scenarios import Scenario  # noqa: E402


class Replay:
    def __init__(self, rows):
        self.turns, cur = [], None
        for r in rows:
            if r.get("kind") == "customer":
                cur = {"tools": [], "reply": None}
                self.turns.append(cur)
            elif cur is not None and r.get("kind") == "tool":
                cur["tools"].append((r["name"], r["args"]))
            elif cur is not None and r.get("kind") == "reply":
                cur["reply"] = r["text"]
        self.i = 0

    def reset(self, system, tools):
        pass

    def respond(self, text, call):
        if self.i >= len(self.turns) or self.turns[self.i]["reply"] is None:
            if self.i < len(self.turns):  # the recording ends mid-turn: replay its tool calls, then stop
                for name, args in self.turns[self.i]["tools"]:
                    call(name, args)
            self.i = len(self.turns) + 1
            raise RuntimeError("recording ended")
        turn = self.turns[self.i]
        self.i += 1
        for name, args in turn["tools"]:
            call(name, args)
        return turn["reply"]


def rescore(out: Path, tasks: Path) -> dict:
    verdicts = {}
    for traj in sorted(glob.glob(f"{out}/*/**/trajectory.jsonl", recursive=True)):
        tid = Path(traj).relative_to(out).parts[0]
        scenario = tasks / tid / "tests" / "scenario.json"
        if not scenario.exists():
            continue
        sc = Scenario(**json.loads(scenario.read_text()))
        rows = [json.loads(line) for line in open(traj)]
        result = run_episode(Replay(rows), sc)
        cut = any(m["role"] == "assistant" and m["text"].startswith("[AGENT ERROR") for m in result["transcript"])
        if cut:  # everything after the recording ended was not the agent's: those tickets were not reached
            for t in result["score"].get("tickets", []):
                pass
        verdicts[tid] = {**result["score"], "ledger": result["ledger"][sc.meta.get("prior_ledger", 0):],
                         "recording_ended_early": cut}
    return verdicts


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("out", type=Path)
    ap.add_argument("--tasks", type=Path, default=ROOT / "tasks-hard")
    ap.add_argument("--json", type=Path, default=None, help="write a results file that scripts/tally.py reads")
    args = ap.parse_args()
    verdicts = rescore(args.out, args.tasks)
    passed = sum(bool(v["pass"]) for v in verdicts.values())
    print(f"{args.out}: {passed}/{len(verdicts)} pass")
    if args.json:
        doc = {"evaluation_results": {tid: {"metadata": {"verifier_output": "FAKECO_SCORE " + json.dumps(
            {"score": {k: v for k, v in s.items() if k not in ("ledger",)}})}} for tid, s in verdicts.items()}}
        args.json.write_text(json.dumps(doc))
