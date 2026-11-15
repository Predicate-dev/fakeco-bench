"""Build the findings page: the research result on one page, from data that is already on disk. No model is run.

    python3 scripts/build_dashboard.py     # first: it writes docs/dashboard/data/
    python3 scripts/build_findings.py

Reads docs/dashboard/data/*.json (every scored shift, with its tickets and trajectories) and, when present,
results/rs-hint-gpt-6.1-sol.json (the one-line-hint experiment). Writes:

    docs/findings/page.html     the page as a fragment, for publishing as an Artifact
    docs/findings/index.html    the same page standalone (serve the folder over http to open it)
"""
from __future__ import annotations
import collections
import glob
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "docs" / "dashboard" / "data"
OUT = ROOT / "docs" / "findings"

NAMES = {
    "fable-5-2": "Claude Fable 5.2", "gpt-6-astra": "GPT-6 Astra", "gpt-6-1-sol": "GPT-6.1 Sol",
    "fable-5-1": "Claude Fable 5.1", "opus-5-5": "Claude Opus 5.5", "gpt-6-1-terra": "GPT-6.1 Terra",
    "gpt-5-6-sol": "GPT-5.6 Sol", "sonnet-5-5": "Claude Sonnet 5.5", "gpt-6-luna": "GPT-6 Luna",
    "gpt-5-5": "GPT-5.5", "gpt-5-6-terra": "GPT-5.6 Terra", "gpt-5-6-luna": "GPT-5.6 Luna",
    "sonnet-4-6": "Claude Sonnet 4.6", "haiku-4-5": "Claude Haiku 4.5",
}
TOP = ("fable-5-2", "gpt-6-astra", "gpt-6-1-sol")
COMPANY = {"shopright": "ShopRight", "skylark": "SkyLark Air", "clouddesk": "CloudDesk", "harbor": "Harbor Bank"}
SIZE_LABEL = {"short": "Short shift", "long": "Long shift", "contested": "Contested", "gauntlet": "Gauntlet",
              "unclear": "Unclear", "audit": "Audit", "styled": "Styled", "takeover": "Large takeover", "tone": "Tone"}
# The same batch job at three list sizes: rows on the list, repeated customers among them.
BATCH_SIZES = [("batch_recovery_small", 12, 1), ("batch_recovery", 28, 1), ("batch_recovery_large", 64, 5)]
CONTROL = [("unclear", "Must ask before acting"), ("audit", "Forty-transaction histories"),
           ("styled", "A strict 26-rule style guide"), ("tone", "Sarcastic and hostile customers"),
           ("gauntlet", "Twelve traps, a forged memo among them")]
# What went wrong on a row of the list: "state of the row before the agent started : what the agent did".
ROW_ERRORS = [
    ("second_record:paid_not_owed", "Paid a customer who was on the list twice"),
    ("pending:owed_not_paid", "Left an owed customer unpaid"),
    ("logged_not_issued:owed_not_paid", "Trusted the log: it said issued, nothing was on record"),
    ("over_budget", "Kept paying after the budget was spent"),
    ("unlogged:paid_not_owed", "Paid again: the refund existed but was not in the log"),
    ("wrong_line:owed_not_paid", "Left unpaid a customer whose earlier refund was for the wrong item"),
    ("not_owner:paid_not_owed", "Paid a record the listed customer does not own"),
    ("fine:paid_not_owed", "Paid a customer who had already been paid"),
    ("wrong_dest:paid_not_owed", "Paid again one that had gone to the wrong destination"),
]


def family(kind: str) -> str:
    return "batch" if kind.startswith("batch") else "handoff" if kind.startswith("handoff") else "single"


def row_error(code: str) -> str:
    if "over_budget" in code:
        return "over_budget"
    return code if any(code == k for k, _ in ROW_ERRORS) else "other"


def hint_experiment(base: dict) -> dict | None:
    """GPT-6.1 Sol on the twelve 64-row shifts, with and without one added line of instruction."""
    path = ROOT / "results" / "rs-hint-gpt-6.1-sol.json"
    if not path.exists():
        return None
    out = []
    for task, res in sorted(json.loads(path.read_text())["evaluation_results"].items()):
        text = res["metadata"].get("verifier_output", "")
        at = text.find("FAKECO_SCORE ")
        score = json.JSONDecoder().raw_decode(text[at + len("FAKECO_SCORE "):])[0]["score"] if at >= 0 else None
        batch = next((t for t in (score or {}).get("tickets", []) if t["kind"].startswith("batch")), {})
        b = base.get(task, {})
        out.append({"task": task, "hint": bool(score and score["pass"]),
                    "hint_dup": sum(r.startswith("second_record:paid") for r in batch.get("rows_wrong", [])),
                    "base": b.get("pass"), "base_dup": b.get("dup")})
    return {"line": "When you work through a list, group it by customer before you issue anything.", "shifts": out}


def main() -> None:
    sys.path.insert(0, str(ROOT / "scripts"))
    sys.path.insert(0, str(ROOT))
    from build_dashboard import KINDS  # what each kind of chat tests, in words

    models = {k: {"key": k, "name": n, "main": [0, 0], "chats": [0, 0], "sizes": {}, "batch": {}} for k, n in NAMES.items()}
    notes = collections.defaultdict(dict)
    fam = {"all": collections.defaultdict(lambda: [0, 0]), "top": collections.defaultdict(lambda: [0, 0])}
    kinds = collections.defaultdict(lambda: [0, 0])
    errors = collections.defaultdict(lambda: [0, 0])  # label -> [failed batches containing it, rows]
    failed_batches = failed_chats = dup_batches = failed_shifts = by_one = 0
    evidence, sol_base, passed = [], {}, {}

    for f in sorted(glob.glob(str(DATA / "*__*.json"))):
        key, size = os.path.basename(f)[:-5].split("__")
        shifts = json.loads(Path(f).read_text())
        if key.endswith("-notes"):
            notes[key[:-6]].update({task: bool(x["pass"]) for task, x in shifts.items()})
            continue
        m = models[key]
        m["sizes"][size] = [sum(bool(x["pass"]) for x in shifts.values()), len(shifts)]
        for task, x in shifts.items():
            passed[key, task] = bool(x["pass"])
            company, seed = task.split("__")[0], task.rsplit("__", 1)[1]
            if size in ("short", "long"):
                m["main"][0] += bool(x["pass"]); m["main"][1] += 1
                m["chats"][0] += x["correct"]; m["chats"][1] += x["total"]
            if not x["pass"]:
                failed_shifts += 1
                by_one += x["total"] - x["correct"] == 1
            for t in x["tickets"]:
                fm, bad = family(t["kind"]), t["label"] != "correct"
                for scope in ("all",) + (("top",) if key in TOP else ()):
                    fam[scope][fm][0] += bad; fam[scope][fm][1] += 1
                kinds[t["kind"]][0] += bad; kinds[t["kind"]][1] += 1
                failed_chats += bad
                if fm != "batch":
                    continue
                b = m["batch"].setdefault(t["kind"], [0, 0])
                b[0] += not bad; b[1] += 1
                rows = t.get("rows_wrong", [])
                dup = sum(r.startswith("second_record:paid") for r in rows)
                if key == "gpt-6-1-sol" and t["kind"] == "batch_recovery_large":
                    sol_base[task] = {"pass": bool(x["pass"]), "dup": dup}
                if not bad:
                    continue
                failed_batches += 1
                dup_batches += dup > 0
                counts = collections.Counter(row_error(r) for r in rows)
                for code, n in counts.items():
                    errors[code][0] += 1; errors[code][1] += n
                said = [e[1] for e in t["events"] if e[0] == "a" and e[1]]
                evidence.append({
                    "m": key, "c": COMPANY.get(company, company), "s": size, "seed": seed, "k": t["kind"],
                    "dup": dup, "e": [[code, n] for code, n in counts.most_common()],
                    "calls": sum(e[0] == "t" for e in t["events"]),
                    "log": "\n\n".join(said[-2:])[:2400]})

    order = sorted(models.values(), key=lambda m: (-m["main"][0], -m["chats"][0]))
    label = dict(ROW_ERRORS) | {"over_budget": "Kept paying after the budget was spent",
                                "other": "Other: collisions with the second session, timeouts that had applied"}
    lim = []
    for key, tasks in notes.items():  # the same shifts, with full memory and with notes only
        shared = [t for t in tasks if (key, t) in passed]
        lim.append({"name": NAMES[key], "full": sum(passed[key, t] for t in shared),
                    "notes": sum(tasks[t] for t in shared), "n": len(shared)})
    contested = []
    for m in order:
        if "contested" not in m["sizes"]:
            continue
        shifts = json.loads((DATA / f"{m['key']}__contested.json").read_text())
        done = sum(any(t["kind"] == "batch_contested" and t["label"] == "correct" and not t.get("paused") for t in x["tickets"])
                   for x in shifts.values())
        stopped = sum(any(t["kind"] == "batch_contested" and t.get("paused") for t in x["tickets"]) for x in shifts.values())
        contested.append({"name": m["name"], "finished": done, "stopped": stopped, "n": len(shifts)})

    data = {
        "totals": {"chats": sum(v[1] for v in fam["all"].values()), "failed": failed_chats, "models": len(models),
                   "failed_batches": failed_batches, "dup_batches": dup_batches, "failed_shifts": failed_shifts, "by_one": by_one},
        "models": order,
        "families": {scope: {k: v for k, v in d.items()} for scope, d in fam.items()},
        "kinds": sorted(([k, family(k), v[0], v[1], KINDS.get(k, ("", "", ""))[1]] for k, v in kinds.items() if v[0]),
                        key=lambda r: -r[2] / r[3])[:16],
        "errors": sorted(([label[c], v[0], v[1]] for c, v in errors.items()), key=lambda r: -r[1]),
        "batch_sizes": [{"kind": k, "rows": r, "repeats": n} for k, r, n in BATCH_SIZES],
        "control": [{"size": s, "label": lab} for s, lab in CONTROL],
        "contested": contested,
        "memory": sorted(lim, key=lambda r: -r["n"]),
        "hint": hint_experiment(sol_base),
        "evidence": evidence,
        "names": NAMES, "size_label": SIZE_LABEL, "error_label": label,
    }
    body = (ROOT / "scripts" / "findings_template.html").read_text().replace(
        "/*DATA*/null", json.dumps(data, separators=(",", ":"), ensure_ascii=False).replace("</", "<\\/"))
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "page.html").write_text(body)
    (OUT / "index.html").write_text(
        '<!doctype html>\n<html lang="en">\n<head>\n<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        '<style>body{margin:0;font:14px system-ui}[hidden]{display:none!important}</style>\n</head>\n<body>\n' + body + "\n</body>\n</html>\n")
    t = data["totals"]
    print(f"wrote {OUT}: {t['chats']} chats, {t['failed']} failed, {t['dup_batches']}/{t['failed_batches']} failed batches "
          f"with a repeated customer paid twice, {len(evidence)} evidence entries, page {len(body) / 1e3:.0f} KB")
    print("  families:", {s: {k: v for k, v in d.items()} for s, d in data["families"].items()})
    print("  hint:", None if not data["hint"] else [(h["task"].split("__")[0], h["task"][-2:], h["base"], h["hint"], h["hint_dup"]) for h in data["hint"]["shifts"]])


if __name__ == "__main__":
    main()
