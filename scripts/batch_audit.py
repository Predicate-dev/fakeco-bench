"""For each long shift in a results file, show what the agent did to each record in the recovery batch.

    valkyrie run outputs RUN_ID --output-dir results/out-opus
    python3 scripts/batch_audit.py results/hard2-opus-5-5.json results/out-opus
"""
import json
import re
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from fakeco.scenarios import make_scenario  # noqa: E402
from fakeco.worlds import WORLDS  # noqa: E402

index = {r["task_id"]: r for r in json.loads((ROOT / "tasks-hard-index.json").read_text())}
path, outputs = sys.argv[1], Path(sys.argv[2])
if True:
    data, tally, esc = json.load(open(path)), Counter(), Counter()
    for tid, entry in sorted(data["evaluation_results"].items()):
        row = index[tid]
        if row["dials"]["size"] != "long":
            continue
        sc = make_scenario(row["world"], "shift", row["dials"], row["seed"])
        w = WORLDS[sc.world]
        ticket = sc.meta["tickets"][-1]
        note = sc.notes[str(ticket["first_turn"])]
        logged = set(re.search(r"\nissued: (.*)\n", note).group(1).split(", "))
        owed = {e[0] for e in ticket["expect"]}
        prior = Counter()
        for t in sc.db["ledger"]:
            prior[(t[w.rid], t["line_item_id"], t[w.dest_param])] += t["quantity"]
        emails = Counter(sc.db["records"][r]["customer_id"] for r in ticket["records"])

        def kind(rec):
            p = {k[1:]: v for k, v in prior.items() if k[0] == rec}
            if emails[sc.db["records"][rec]["customer_id"]] > 1 and not p:
                return "second_record"
            if not p:
                return "logged_not_issued" if rec in logged else "pending"
            if p == {("L2", w.default_method): 2}:
                return "double"
            if ("L1", w.default_method) in p:
                return "wrong_line"
            if ("L2", w.alt_method) in p:
                return "wrong_dest"
            return "fine" if rec in logged else "unlogged"

        # Long shifts overflow the verifier's log, so read the agent's own trajectory for the tool calls.
        traj = next((outputs / tid).rglob("trajectory.jsonl"), None)
        if traj is None:
            print(f"{tid}: no trajectory under {outputs}")
            continue
        rows = [json.loads(line) for line in traj.read_text().splitlines()]
        start = max(i for i, r in enumerate(rows) if r["kind"] == "customer" and "No customer is waiting" in r["text"])
        new, escalated = Counter(), False
        for r in rows[start:]:
            if r["kind"] != "tool":
                continue
            if r["name"] == "escalate_to_human":
                escalated = True
            result = json.loads(r["result"])
            if r["name"] == w.money_tool and result.get("status") == "completed":
                new[r["args"].get(w.rid)] += result["quantity"]
        label = next(t["label"] for t in json.loads([l for l in entry["metadata"]["verifier_output"].splitlines()
                     if l.startswith("FAKECO_SCORE ")][-1][13:])["score"]["tickets"] if t["kind"] == "batch_recovery")
        wrong = []
        for rec in ticket["records"]:
            k = kind(rec)
            want = 1 if rec in owed else 0
            tally[(k, "ok" if new[rec] == want else ("paid_not_owed" if new[rec] > want else "owed_not_paid"))] += 1
            if new[rec] != want:
                wrong.append(f"{k}:{'+' if new[rec] > want else '-'}")
        esc[escalated] += 1
        print(f"{tid}: {label}  {' '.join(wrong) or '(payments right)'}{'' if escalated else '  [no escalation]'}")
    print(f"\n{Path(path).stem}: per record kind")
    for k in ("fine", "logged_not_issued", "pending", "unlogged", "double", "wrong_dest", "wrong_line", "second_record"):
        cells = {o: tally[(k, o)] for o in ("ok", "paid_not_owed", "owed_not_paid") if tally[(k, o)]}
        print(f"  {k:<18} {cells}")
