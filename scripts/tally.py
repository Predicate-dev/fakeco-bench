"""Per-ticket tally of one or more Valkyrie results files: python3 scripts/tally.py results/run.json ..."""
import collections
import json
import sys


def score(entry):
    out = (entry.get("metadata") or {}).get("verifier_output") or ""
    lines = [l for l in out.splitlines() if l.startswith("FAKECO_SCORE ")]
    return json.loads(lines[-1][len("FAKECO_SCORE "):])["score"] if lines else None


for path in sys.argv[1:]:
    data = json.load(open(path))
    sizes, kinds, rows, ungraded = collections.defaultdict(lambda: [0, 0]), collections.defaultdict(lambda: [0, 0]), collections.Counter(), 0
    labels = collections.Counter()
    for tid, entry in sorted(data.get("evaluation_results", {}).items()):
        s = score(entry)
        if s is None:
            ungraded += 1
            continue
        size = tid.split("size-")[1].split("__")[0] if "size-" in tid else "core"
        sizes[size][0] += bool(s.get("pass"))
        sizes[size][1] += 1
        for t in s.get("tickets", []):
            kinds[(size, t["kind"])][0] += t["label"] == "correct"
            kinds[(size, t["kind"])][1] += 1
            if t["label"] != "correct":
                labels[(size, t["kind"], t["label"])] += 1
                for r in t.get("rows_wrong", []):
                    rows[(size, r)] += 1
    print(f"== {path}: " + ", ".join(f"{k} {v[0]}/{v[1]}" for k, v in sorted(sizes.items())) + (f", ungraded {ungraded}" if ungraded else ""))
    for (size, kind), (ok, n) in sorted(kinds.items()):
        if ok < n:
            print(f"   {size:9s} {kind:32s} {ok}/{n}  " + ", ".join(f"{l} x{c}" for (s2, k2, l), c in labels.items() if (s2, k2) == (size, kind)))
    for (size, r), c in sorted(rows.items()):
        print(f"      row {size:9s} {r} x{c}")
