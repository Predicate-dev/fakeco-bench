"""Print one task's chat and tool calls from a Valkyrie results file.

    python3 scripts/show.py results/run.json TASK_ID_SUBSTRING
"""
import json
import sys

data = json.load(open(sys.argv[1]))
for tid, entry in sorted(data.get("evaluation_results", {}).items()):
    if len(sys.argv) > 2 and sys.argv[2] not in tid:
        continue
    out = (entry.get("metadata") or {}).get("verifier_output") or ""
    try:
        verdict = json.JSONDecoder().raw_decode(out[out.index("{"):])[0]
    except ValueError:
        print(f"== {tid}: no verdict in verifier output\n{out[-800:]}")
        continue
    print(f"== {tid}\n   {json.dumps(verdict['score'])}")
    events = [("chat", m["ts"], m) for m in verdict["transcript"]] + [("tool", e["ts"], e) for e in verdict["tool_log"]]
    for kind, _, e in sorted(events, key=lambda x: x[1]):
        if kind == "chat":
            print(f"\n[{'CUSTOMER' if e['role'] == 'user' else 'AGENT'}] {e['text']}")
        else:
            flag = " (FAULT INJECTED)" if e["faulted"] else ""
            print(f"\n   -> {e['tool']}({json.dumps(e['args'])}){flag}\n   <- {json.dumps(e['result'])[:400]}")
    print()
