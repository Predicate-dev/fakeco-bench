"""Show one ticket of one shift from saved run outputs: python3 scripts/ticket.py OUTPUT_DIR TASK_ID KIND"""
import glob
import json
import sys

out, tid, kind = sys.argv[1:4]
sc = json.load(open(f"tasks-hard/{tid}/tests/scenario.json"))
rows = [json.loads(l) for l in open(glob.glob(f"{out}/{tid}/**/trajectory.jsonl", recursive=True)[0])]
k = [t["kind"] for t in sc["meta"]["tickets"]].index(kind)
first = sc["user_turns"][sc["meta"]["tickets"][k]["first_turn"]]
nxt = sc["user_turns"][sc["meta"]["tickets"][k + 1]["first_turn"]] if k + 1 < len(sc["meta"]["tickets"]) else None
on = False
for r in rows:
    if r.get("kind") == "customer":
        text = r.get("text", "")
        if text.endswith(first):
            on = True
        elif nxt and text.endswith(nxt):
            on = False
        if on:
            print("CUSTOMER:", text[-700:].replace("\n", " | "))
    elif on and r.get("kind") == "tool":
        print("   TOOL", r["name"], json.dumps(r["args"])[:160], "->", str(r["result"]).replace("\n", " ")[:260])
    elif on and r.get("kind") == "reply":
        print("AGENT:", r["text"][:500].replace("\n", " "))
