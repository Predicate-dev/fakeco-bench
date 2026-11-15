"""Print each task's verdict line from a Valkyrie results file: python3 scripts/verdicts.py results/run.json"""
import json
import sys

for path in sys.argv[1:]:
    data = json.load(open(path))
    for tid, entry in sorted(data.get("evaluation_results", {}).items()):
        out = (entry.get("metadata") or {}).get("verifier_output") or ""
        lines = [l for l in out.splitlines() if l.startswith("FAKECO_SCORE ")]
        score = json.loads(lines[-1][len("FAKECO_SCORE "):])["score"] if lines else {"label": "not_graded"}
        print(f"{'PASS' if score.get('pass') else 'FAIL'}  {tid}\n      {json.dumps(score)}")
