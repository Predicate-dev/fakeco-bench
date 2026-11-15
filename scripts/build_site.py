"""Build the public FakeCo Bench page from data that is already on disk. No model is run.

    python3 scripts/build_dashboard.py     # first, so docs/dashboard is current
    python3 scripts/build_site.py

Reads the dashboard index (docs/dashboard/page.html), the cost estimates (results/final/pareto.json, optional),
three example shifts (tasks-hard/*/tests/scenario.json) and three failure excerpts (docs/dashboard/data/*.json),
and writes:

    docs/site/index.html    the page, standalone (serve the folder over http, or open the file)
    docs/site/page.html     the same page as a fragment, for publishing as an Artifact

Every number on the page comes from those files. A story whose source run is missing is left out, not invented.
"""
from __future__ import annotations
import datetime
import json
import math
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DASH, TASKS, OUT = ROOT / "docs" / "dashboard", ROOT / "tasks-hard", ROOT / "docs" / "site"
COMPANIES = {"shopright": "ShopRight", "skylark": "SkyLark Air", "clouddesk": "CloudDesk", "harbor": "Harbor Bank"}
MONEY = ("issue_refund", "issue_credit", "reverse_fee")
EXAMPLES = ["harbor__shift__size-long__s0", "shopright__shift__size-gauntlet__s0", "clouddesk__shift__size-contested__s1"]


def dashboard_index() -> dict:
    text = (DASH / "page.html").read_text()
    return json.loads(text.split("const INDEX = ")[1].split(";\nconst WORLDS")[0])


def example(tid: str, kinds: dict) -> dict | None:
    path = TASKS / tid / "tests" / "scenario.json"
    if not path.exists():
        return None
    sc = json.loads(path.read_text())
    intro, _, book = sc["system"].partition("AGENT HANDBOOK\n")
    rules = []
    for line in book.splitlines():
        m = re.match(r"(\d+)\. ([^.]+)\. (.*)", line)
        if m:
            rules.append({"n": int(m.group(1)), "title": m.group(2), "text": m.group(3)})
    chats = []
    for i, t in enumerate(sc["meta"]["tickets"]):
        note = re.sub(r"^Chat \d+ of \d+\.\s*", "", sc["notes"].get(str(t["first_turn"]), ""))
        info = kinds.get(t["kind"], {})
        chats.append({"n": i + 1, "kind": t["kind"], "family": info.get("family", ""), "what": info.get("what", ""),
                      "right": info.get("right", ""), "opening": sc["user_turns"][t["first_turn"]], "note": note,
                      "turns": t["n_turns"]})
    world, _, size, seed = tid.split("__")
    return {"id": tid, "company": COMPANIES.get(world, world), "size": size.replace("size-", ""),
            "seed": seed.replace("s", ""), "intro": intro.strip(), "rules": rules, "chats": chats}


def chat_counts(sizes: set) -> dict:
    """How many chats a shift of each graded type has, read from one generated shift of each."""
    out = {}
    for path in sorted(TASKS.glob("harbor__shift__size-*__s0/tests/scenario.json")):
        size = path.parts[-3].split("size-")[1].split("__")[0]
        if size in sizes:
            out[size] = len(json.loads(path.read_text())["meta"]["tickets"])
    return out


def load(name: str, shift: str) -> dict | None:
    path = DASH / "data" / name
    if not path.exists():
        return None
    return json.loads(path.read_text()).get(shift)


def plain(text: str) -> str:
    return text.replace("**", "")


def story_duplicate() -> dict | None:
    """Claude Opus 5.5 pays a customer who is on the batch list twice."""
    shift = load("opus-5-5__long.json", "harbor__shift__size-long__s0")
    if not shift:
        return None
    ticket = shift["tickets"][-1]
    note = next((e[2] for e in ticket["events"] if e[0] == "c" and "The list:" in (e[2] or "")), None)
    if not note or "second_record:paid_not_owed" not in ticket.get("rows_wrong", []):
        return None
    rows = [re.match(r"(\d+)\. (\S+) / (\S+)", l) for l in note.split("The list:\n")[1].splitlines()]
    rows = [(int(m.group(1)), m.group(2), m.group(3)) for m in rows if m]
    emails = [r[1] for r in rows]
    twice = [r for r in rows if emails.count(r[1]) == 2]
    paid = {e[2].get("statement_id"): e for e in ticket["events"] if e[0] == "t" and e[1] in MONEY
            and isinstance(e[3], dict) and e[3].get("status") == "completed"}
    agent_row = next((r for r in twice if r[2] in paid), None)
    summary = plain(next((e[1] for e in reversed(ticket["events"]) if e[0] == "a" and "would have taken the total" in e[1]), ""))
    budget = re.search(r"total budget of \$(\d+\.\d\d)", note)
    over = re.search(r"#(\d+) (\S+) \(\$([\d.]+)\) would have taken the total to \$([\d.]+)", summary)
    if not (agent_row and budget and over and len(twice) == 2):
        return None
    other = next(r for r in twice if r is not agent_row)
    stopped = next(l for l in summary.splitlines() if "would have taken the total" in l)
    return {
        "key": "duplicate", "model": "Claude Opus 5.5", "where": f"Harbor Bank, long shift, chat {len(shift['tickets'])} of {len(shift['tickets'])}",
        "rows": len(rows), "budget": budget.group(1), "would_be": over.group(4),
        "tape": [
            {"n": agent_row[0], "email": agent_row[1], "record": agent_row[2], "amount": paid[agent_row[2]][3].get("amount"),
             "stamp": "Paid by the agent", "bad": True},
            {"n": other[0], "email": other[1], "record": other[2], "amount": None, "stamp": "Already paid before the crash", "bad": False},
            {"n": int(over.group(1)), "email": "", "record": over.group(2), "amount": float(over.group(3)),
             "stamp": "Owed, left unpaid", "bad": True},
        ],
        "events": [
            ["n", "The batch list, rows {} and {} of {}".format(*sorted([agent_row[0], other[0]]), len(rows)),
             "\n".join(f"{r[0]}. {r[1]} / {r[2]}" for r in sorted(twice))],
            ["t", paid[agent_row[2]][1], paid[agent_row[2]][2], paid[agent_row[2]][3]],
            ["a", stopped.strip(), True],
        ],
    }


def story_resumed() -> dict | None:
    """Claude Sonnet 5.5 pays rows the colleague's resumed session had already paid."""
    path = DASH / "data" / "sonnet-5-5__contested.json"
    if not path.exists():
        return None
    for tid, shift in json.loads(path.read_text()).items():
        ticket = shift["tickets"][-1]
        reply = next((e[1] for e in ticket["events"] if e[0] == "a" and "became duplicates" in e[1]), None)
        if not reply:
            continue
        para = plain(next(p for p in reply.split("\n\n") if "became duplicates" in p))
        para = para[:para.index("became duplicates") + len("became duplicates")] + "."
        notice = shift["tickets"][0]["events"][0][2]
        notice = re.sub(r"^Chat \d+ of \d+\.\s*", "", notice)
        notice = notice[:notice.index("without telling anyone.") + len("without telling anyone.")] if "without telling anyone." in notice else notice
        tools = [e[1] for e in ticket["events"] if e[0] == "t"]
        run = best = 0
        for name in tools:  # longest run of payments with no read in between
            run = run + 1 if name in MONEY else 0
            best = max(best, run)
        world = tid.split("__")[0]
        return {"key": "resumed", "model": "Claude Sonnet 5.5",
                "where": f"{COMPANIES[world]}, contested shift, chat {len(shift['tickets'])} of {len(shift['tickets'])}",
                "burst": best, "reads": sum(1 for n in tools if n.startswith("get_") and n != "get_transactions"),
                "events": [["n", "Internal note with chat 1", notice], ["gap", f"{len(shift['tickets']) - 1} chats later, in the batch"],
                           ["a", para, True]]}
    return None


def story_ambiguous() -> dict | None:
    """GPT-6.1 Terra refunds every line that could match an ambiguous request."""
    path = DASH / "data" / "gpt-6-1-terra__gauntlet.json"
    if not path.exists():
        return None
    for tid, shift in sorted(json.loads(path.read_text()).items()):
        if not tid.startswith("shopright"):
            continue
        for ticket in shift["tickets"]:
            if ticket["kind"] != "ambiguous_item" or ticket["label"] == "correct":
                continue
            paid = [e[3].get("amount", 0) for e in ticket["events"] if e[0] == "t" and e[1] in MONEY
                    and isinstance(e[3], dict) and e[3].get("status") == "completed"]
            if len(paid) < 3:
                continue
            events = [(["c", e[1], ""] if e[0] == "c" else e) for e in ticket["events"]]
            return {"key": "ambiguous", "model": "GPT-6.1 Terra", "where": "ShopRight, gauntlet shift",
                    "paid": round(sum(paid), 2), "wanted": min(paid), "events": events}
    return None


def main() -> None:
    index = dashboard_index()
    pareto_path = ROOT / "results" / "final" / "pareto.json"
    pareto = json.loads(pareto_path.read_text()) if pareto_path.exists() else {}
    models = []
    for m in index["models"]:
        p = pareto.get(m["key"], {})
        models.append({k: m[k] for k in ("key", "name", "provider", "tiers", "main", "tickets", "families", "finished",
                                         "seeds", "calls", "errors", "rows")}
                      | {"cost": p.get("cost_shift"), "minutes": p.get("min_shift")})
    shift_ids = {s["id"] for m in index["models"] for s in m["shifts"]}
    totals = {
        "models": len(models), "shifts": len(shift_ids),
        "companies": len({s.split("__")[0] for s in shift_ids}),
        "types": len({s.split("size-")[1].split("__")[0] for s in shift_ids}),
        "graded": sum(len(m["shifts"]) for m in index["models"]),
        "chats": sum(f[1] for m in index["models"] for f in m["families"].values()),
    }
    stories = [s for s in (story_duplicate(), story_resumed(), story_ambiguous()) if s]
    data = {
        "built": datetime.date.today().isoformat(), "totals": totals, "models": models, "families": index["families"],
        "examples": [e for e in (example(t, index["kinds"]) for t in EXAMPLES) if e],
        "chat_counts": chat_counts({s.split("size-")[1].split("__")[0] for s in shift_ids}), "stories": stories,
    }
    body = (ROOT / "scripts" / "site_template.html").read_text().replace(
        "/*DATA*/null", json.dumps(data, separators=(",", ":"), ensure_ascii=False).replace("</", "<\\/"))
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "page.html").write_text(body)
    (OUT / "index.html").write_text(
        '<!doctype html>\n<html lang="en">\n<head>\n<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n<style>body{margin:0}</style>\n</head>\n<body>\n'
        + body + "\n</body>\n</html>\n")
    print(f"wrote {OUT}: {totals}, stories {[s['key'] for s in stories]}, examples {len(data['examples'])}, "
          f"page {len(body) / 1e3:.0f} KB")
    for m in models:
        n = m["main"][1] or 1
        p = m["main"][0] / n
        print(f"  {m['name']:18s} {m['main'][0]}/{m['main'][1]}  ±{100 * math.sqrt(p * (1 - p) / n):.1f}  cost {m['cost']}")


if __name__ == "__main__":
    main()
