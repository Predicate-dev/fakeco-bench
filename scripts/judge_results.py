"""Apply the LLM judge to a downloaded Valkyrie results file.

On Trackers that do not pass task secrets to the verifier, the two text-graded
modes (silent_tool_failure, stale_document) are scored in the sandbox by regex.
This re-grades those tasks from the transcript and tool log each verdict carries,
with the same judge and rubric as tests/judge.py, and writes the verdicts next to
the results file as NAME.judged.json. scripts/report.py picks that file up.

    export ANTHROPIC_API_KEY=...
    python3 scripts/judge_results.py results/sonnet.json
"""
from __future__ import annotations
import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "harbor" / "verifier"))

os.environ.setdefault("FAKECO_JUDGE_API_KEY", os.environ.get("ANTHROPIC_API_KEY", ""))

import judge  # noqa: E402
from fakeco.worlds import WORLDS  # noqa: E402

TEXT_GRADED = ("silent_tool_failure", "stale_document")


def verdict_of(entry: dict) -> dict | None:
    out = (entry.get("metadata") or {}).get("verifier_output") or ""
    try:
        return json.JSONDecoder().raw_decode(out[out.index("{"):])[0]
    except ValueError:
        return None


def regrade(item: tuple[str, dict]) -> tuple[str, dict] | None:
    tid, entry = item
    verdict = verdict_of(entry)
    if not verdict or verdict["mode"] not in TEXT_GRADED:
        return None
    score = verdict["score"]
    if score["label"] in ("no_reply", "abandoned_chat") or score.get("graded_by", "").startswith("judge:"):
        return None
    regex = {k: v for k, v in score.items() if k not in ("graded_by", "judge", "judge_error")}
    scenario = {"mode": verdict["mode"], "dials": verdict["dials"]}
    return tid, judge.apply(WORLDS[verdict["world"]], scenario, verdict["transcript"], verdict["tool_log"], regex)


def main() -> int:
    if not judge.available():
        sys.exit("Set ANTHROPIC_API_KEY (or FAKECO_JUDGE_API_KEY) to run the judge.")
    for path in map(Path, sys.argv[1:]):
        results = json.loads(path.read_text()).get("evaluation_results", {})
        with ThreadPoolExecutor(max_workers=6) as pool:
            judged = dict(r for r in pool.map(regrade, results.items()) if r)
        out = path.with_suffix(".judged.json")
        out.write_text(json.dumps(judged, indent=2))
        flips = [t for t, s in judged.items() if s.get("regex_pass") is not None and s["pass"] != s["regex_pass"]]
        errors = [t for t, s in judged.items() if "judge_error" in s]
        print(f"{path.name}: judged {len(judged)} tasks, {len(flips)} verdicts changed, {len(errors)} judge errors -> {out}")
        for t in flips:
            print(f"  changed: {t}: regex={judged[t]['regex_label']} judge={judged[t]['label']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
