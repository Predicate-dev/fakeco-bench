"""Build the public Exactly Once page from data that is already on disk. No model is run.

    python3 scripts/build_dashboard.py     # first, so docs/dashboard is current
    python3 scripts/build_site.py

Reads the dashboard index (docs/dashboard/page.html), the cost estimates (results/final/pareto.json, optional),
one generated short shift for the playable batch (tasks-hard/.../tests/scenario.json) and one saved run for the
replay (docs/dashboard/data/opus-5-5__long.json), and writes:

    docs/site/index.html    the page, standalone
    docs/site/page.html     the same page as a fragment, for publishing as an Artifact

Every number on the page comes from those files. The playable batch embeds that one batch's answer key, because
the page grades it; the replay is left out if its source run is missing.
"""
from __future__ import annotations
import datetime
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from fakeco.worlds import WORLDS  # noqa: E402

DASH, TASKS, OUT = ROOT / "docs" / "dashboard", ROOT / "tasks-hard", ROOT / "docs" / "site"
COMPANIES = {"shopright": "ShopRight", "skylark": "SkyLark Air", "clouddesk": "CloudDesk", "harbor": "Harbor Bank"}
MONEY = ("issue_refund", "issue_credit", "reverse_fee")
GAME_SHIFT = "harbor__shift__size-short__s0"
REPLAY = ("opus-5-5__long.json", "harbor__shift__size-long__s0", "Claude Opus 5.5")
REPEAT = ("fable-5-2__takeover.json", "skylark__shift__size-takeover__s1", "Claude Fable 5.2")


def dashboard_index() -> dict:
    text = (DASH / "page.html").read_text()
    return json.loads(text.split("const INDEX = ")[1].split(";\nconst WORLDS")[0])


def parse_note(note: str) -> dict:
    """The batch task as the agent gets it: the brief, the colleague's log, and the list."""
    note = re.sub(r"^Chat \d+ of \d+\.\s*", "", note)
    brief, rest = re.split(r"\n\n\w+'s log says:\n", note, maxsplit=1)
    log, listing = rest.split("\n\nThe list:\n")
    issued = re.search(r"^issued: (.*)$", log, re.M).group(1).split(", ")
    pending = re.search(r"^not yet issued: (.*)$", log, re.M).group(1).split(", ")
    rows = [re.match(r"(\d+)\. (\S+) / (\S+)", line) for line in listing.splitlines()]
    return {"brief": brief, "issued": issued, "pending": pending,
            "rows": [(int(m.group(1)), m.group(2), m.group(3)) for m in rows if m]}


def game() -> dict | None:
    """One real 12-row takeover batch, with the records a visitor needs to decide each row and the key to grade it."""
    path = TASKS / GAME_SHIFT / "tests" / "scenario.json"
    if not path.exists():
        return None
    sc = json.loads(path.read_text())
    ticket = sc["meta"]["tickets"][-1]
    if ticket["kind"] != "batch_recovery_small":
        return None
    w, db = WORLDS[sc["world"]], sc["db"]
    note = parse_note(sc["notes"][str(ticket["first_turn"])])
    by_email = {c["email"]: c for c in db["customers"].values()}
    kinds = {r["record"]: r["kind"] for r in ticket["rows"]}
    owed = {e[0] for e in ticket["expect"]}
    rows = []
    for n, email, rec_id in note["rows"]:
        cust, rec = by_email[email], db["records"][rec_id]
        owns = [r["id"] for r in db["records"].values() if r["customer_id"] == cust["id"]]
        tx = [{"id": t["transaction_id"], "record": t[w.rid], "line": t["line_item_id"], "qty": t["quantity"],
               "to": t[w.dest_param], "amount": t["amount"], "days_ago": t.get("days_ago", 0)}
              for t in db["ledger"] if t[w.rid] in owns]
        rows.append({
            "n": n, "email": email, "record": rec_id, "logged": "issued" if rec_id in note["issued"] else "not yet issued",
            "customer": {"id": cust["id"], "name": cust["name"], "owns": owns},
            "rec": {"owner": rec["customer_id"], "payment": rec["payment_method"],
                    "lines": [{k: l[k] for k in ("id", "name", "unit_price", "qty", "refunded_qty")} | ({"final": True} if l.get("final") else {})
                              for l in rec["line_items"]]},
            "tx": sorted(tx, key=lambda t: t["days_ago"]), "kind": kinds[rec_id], "owed": rec_id in owed})
    line = next(l["name"] for l in db["records"][note["rows"][0][2]]["line_items"] if l["id"] == "L2")
    return {"shift": f"{COMPANIES[sc['world']]}, short shift, chat {len(sc['meta']['tickets'])} of {len(sc['meta']['tickets'])}",
            "noun": w.noun, "word": w.money_word, "line": line, "brief": note["brief"], "rows": rows}


def replay() -> dict | None:
    """The real events behind one failure, cut into steps. Returns None if any piece cannot be found in the run."""
    name, tid, model = REPLAY
    path = DASH / "data" / name
    if not path.exists():
        return None
    shift = json.loads(path.read_text()).get(tid)
    if not shift:
        return None
    ticket = shift["tickets"][-1]
    ev = ticket["events"]
    raw = next((e[2] for e in ev if e[0] == "c" and "The list:" in (e[2] or "")), None)
    if not raw or "second_record:paid_not_owed" not in ticket.get("rows_wrong", []):
        return None
    note = parse_note(raw)
    emails = [r[1] for r in note["rows"]]
    twice = [r for r in note["rows"] if emails.count(r[1]) == 2]
    tools = [e for e in ev if e[0] == "t"]
    rid = "statement_id"
    pay = {e[2].get(rid): e for e in tools if e[1] in MONEY and isinstance(e[3], dict) and e[3].get("status") == "completed"}
    paid_row = next((r for r in twice if r[2] in pay), None)
    if not paid_row or len(twice) != 2:
        return None
    other = next(r for r in twice if r is not paid_row)
    lookup = next((e for e in tools if e[1] == "lookup_customer" and e[2].get("email") == paid_row[1]), None)
    reads = {e[2].get(rid): e[3] for e in tools if e[1].startswith("get_") and e[2].get(rid) in (paid_row[2], other[2])}
    txs = next((e for e in tools if e[1] == "get_transactions" and isinstance(e[3], dict)
                and any(t.get(rid) == other[2] for t in e[3].get("transactions", []))), None)
    summary = next((e[1] for e in ev if e[0] == "a" and "would have taken the total" in e[1]), "").replace("**", "")
    budget = re.search(r"total budget of \$(\d+\.\d\d)", raw)
    used = re.search(r"\$(\d+\.\d\d) of the \$\d+\.\d\d used", summary)
    over = re.search(r"#(\d+) (\S+) \(\$(\d+\.\d\d)\) would have taken the total to \$(\d+\.\d\d)", summary)
    if not (lookup and len(reads) == 2 and txs and budget and used and over):
        return None
    key = TASKS / tid / "tests" / "scenario.json"  # the row the agent left unpaid has to be one the answer key says was owed
    if not key.exists() or over.group(2) not in {e[0] for e in json.loads(key.read_text())["meta"]["tickets"][-1]["expect"]}:
        return None
    l2 = lambda rec: next(l for l in rec["line_items"] if l["id"] == "L2")
    dup = pay[paid_row[2]][3]
    prior = next(t for t in txs[3]["transactions"] if t.get(rid) == other[2])
    money = lambda v: f"${float(v):.2f}"
    without = float(used.group(1)) - dup["amount"] + float(over.group(3))
    rule = next(s.strip() for s in re.split(r"(?<=\.)\s+", note["brief"]) if "nobody gets two" in s)
    first = summary.split("\n")[0]
    stopped = next(l for l in summary.splitlines() if "would have taken the total" in l)
    rows = sorted(twice)
    steps = [
        {"title": "The job", "blocks": [
            ["p", f"A colleague's batch crashed halfway. {len(note['rows'])} customers, one refund each, a budget of {money(budget.group(1))}."],
            ["quote", rule, "From the task"]]},
        {"title": f"Rows {rows[0][0]} and {rows[1][0]}", "blocks": [
            ["p", "The same email is on the list twice."],
            ["mono", [[f"{r[0]:>2}. {r[1]} / {r[2]}", True] for r in rows]]]},
        {"title": "It looks her up", "blocks": [
            ["p", "One customer. Both listed records are hers."],
            ["call", "lookup_customer", f"email={paid_row[1]}"],
            ["mono", [[f"id            {lookup[3]['id']}", False], [f"name          {lookup[3]['name']}", False],
                      [f"statement_ids {', '.join(lookup[3]['statement_ids'])}", True]]]]},
        {"title": "It reads both records", "blocks": [
            ["p", f"Row {other[0]} already shows one refunded. Row {paid_row[0]} shows none."],
            ["mono", [[f"{r[2]}  {l2(reads[r[2]])['name']}  refunded_qty {l2(reads[r[2]])['refunded_qty']}", r is other] for r in rows]]]},
        {"title": "It reads her transactions", "blocks": [
            ["p", "She was refunded today, before the crash."],
            ["call", "get_transactions", f"customer_id={lookup[3]['id']}"],
            ["mono", [[f"{prior['transaction_id']}  {prior[rid]}  {money(prior['amount'])}  days_ago {prior.get('days_ago', 0)}", True]]]]},
        {"title": f"It pays row {paid_row[0]} anyway", "stamp": ["Paid twice", "red"], "blocks": [
            ["call", pay[paid_row[2]][1], f"{rid}={paid_row[2]} line_item_id=L2 quantity=1"],
            ["mono", [[f"{dup['transaction_id']}  completed  {money(dup['amount'])}", True]]],
            ["p", "Refunds cannot be undone."]]},
        {"title": "The budget", "blocks": [
            ["mono", [[f"used            {money(used.group(1))} of {money(budget.group(1))}", False],
                      [f"row {over.group(1)} would add  {money(over.group(3))}  ->  {money(over.group(4))}", True],
                      [f"without the duplicate: {money(float(used.group(1)) - dup['amount'])} + {money(over.group(3))} = {money(without)}", False]]],
            ["p", f"The duplicate is exactly what pushes row {over.group(1)} over." if without <= float(budget.group(1)) else ""]]},
        {"title": f"Row {over.group(1)} goes unpaid", "stamp": ["Owed, unpaid", "red"], "blocks": [
            ["p", "That customer was owed. The agent stops, as the budget rule says."],
            ["mono", [[f"{over.group(1)}. {over.group(2)}  {money(over.group(3))}  not issued", True]]]]},
        {"title": "Its own summary", "blocks": [
            ["quote", first, f"{model}, to the shift log"],
            ["quote", stopped.rstrip(":"), ""]]},
    ]
    return {"key": "duplicate", "model": model, "title": f"how {model.replace('Claude ', '')} paid one customer twice",
            "where": f"Harbor Bank, long shift, chat {len(shift['tickets'])} of {len(shift['tickets'])}",
            "rows": len(note["rows"]), "steps": steps}


def replay_repeat() -> dict | None:
    """A large batch in which the agent pays customers who are on the list more than once. Every figure is read from
    the saved run and checked against the task's answer key; returns None if any of it is not there."""
    name, tid, model = REPEAT
    path, key = DASH / "data" / name, TASKS / tid / "tests" / "scenario.json"
    if not path.exists() or not key.exists():
        return None
    shift = json.loads(path.read_text()).get(tid)
    sc = json.loads(key.read_text())
    if not shift or shift["tickets"][-1]["label"] == "correct":
        return None
    w = WORLDS[sc["world"]]
    ev = shift["tickets"][-1]["events"]
    raw = next((e[2] for e in ev if e[0] == "c" and "The list:" in (e[2] or "")), None)
    if not raw:
        return None
    note = parse_note(raw)
    row_of = {r[2]: r for r in note["rows"]}
    emails = [r[1] for r in note["rows"]]
    repeats = {e: [r for r in note["rows"] if r[1] == e] for e in dict.fromkeys(emails) if emails.count(e) > 1}
    answer = sc["meta"]["tickets"][-1]
    kinds = {r["record"]: r["kind"] for r in answer["rows"]}
    owed = {e[0] for e in answer["expect"]}
    tools = [e for e in ev if e[0] == "t"]
    paid = {e[2].get(w.rid): e[3] for e in tools if e[1] == w.money_tool and isinstance(e[3], dict) and e[3].get("status") == "completed"}
    wrong = sorted((row_of[r] for r in paid if r not in owed), key=lambda r: r[0])
    missed = sorted((row_of[r] for r in owed if r not in paid), key=lambda r: r[0])
    if not wrong or any(kinds[r[2]] != "second_record" for r in wrong):
        return None
    seen = {}
    for e in tools:  # what the agent saw the first time it read each repeated record
        rec = e[2].get(w.rid)
        if e[1] == f"get_{w.noun}" and isinstance(e[3], dict) and rec in row_of and row_of[rec][1] in repeats and rec not in seen:
            seen[rec] = next(l for l in e[3]["line_items"] if l["id"] == "L2")["refunded_qty"]
    summary = next((e[1] for e in ev if e[0] == "a" and "entries now have exactly one" in e[1]), "").replace("**", "")
    budget = re.search(r"total budget of \$(\d+\.\d\d)", raw)
    headline = re.search(r"The batch is not complete: [^\n]*?exactly \$\d+\.\d\d\.", summary)
    chose = re.search(r"Four customers appear on the list more than once[^\n]*?has one unit\.", summary)
    if not (budget and headline and chose and len(seen) == sum(len(v) for v in repeats.values())) or f"exactly ${budget.group(1)}" not in headline.group(0):
        return None
    money = lambda v: f"${float(v):.2f}"
    price = lambda rec: next(l["unit_price"] for l in sc["db"]["records"][rec]["line_items"] if l["id"] == "L2")
    spent, short = sum(paid[r[2]]["amount"] for r in wrong), sum(price(r[2]) for r in missed)
    rows_text = lambda rs: ", ".join(str(r[0]) for r in rs[:-1]) + (" and " if len(rs) > 1 else "") + str(rs[-1][0])
    rule = next(x.strip() for x in re.split(r"(?<=\.)\s+", note["brief"]) if "nobody gets two" in x)
    had = [r for rs in repeats.values() for r in rs if seen[r[2]]]
    steps = [
        {"title": "The job", "blocks": [
            ["p", f"A colleague's batch crashed halfway. {len(note['rows'])} customers, one refund each, a budget of {money(budget.group(1))}."],
            ["quote", rule, "From the task"]]},
        {"title": f"{len(repeats)} emails repeat", "blocks": [
            ["p", f"{len(repeats)} customers are on the list more than once. One is on it {max(len(v) for v in repeats.values())} times."],
            ["mono", [[f"{r[0]:>2}. {r[1]} / {r[2]}", False] for rs in repeats.values() for r in rs]]]},
        {"title": "What the records showed", "blocks": [
            ["p", f"The agent read every record. {len(had)} of these already showed a refund."],
            ["mono", [[f"row {r[0]:>2}  {r[2]}  refunded_qty {seen[r[2]]}", bool(seen[r[2]])] for rs in repeats.values() for r in rs]]]},
        {"title": "It pays them anyway", "stamp": ["Paid twice", "red"], "blocks": [
            ["p", f"Rows {rows_text(wrong)}: {len(wrong)} payments, {money(spent)}, to customers already covered."],
            ["mono", [[f"{paid[r[2]]['transaction_id']}  row {r[0]:>2}  {r[2]}  {money(paid[r[2]]['amount'])}", True] for r in wrong]]]},
        {"title": "The budget runs out", "stamp": ["Owed, unpaid", "red"], "blocks": [
            ["p", f"{len(missed)} customers who were owed got nothing." + (" Without the repeats, they fit." if short <= spent else "")],
            ["mono", [[f"budget {money(budget.group(1))}, fully used", False]] + [[f"row {r[0]:>2}  {r[2]}  {money(price(r[2]))}  not issued", True] for r in missed]
             + [[f"spent on repeats {money(spent)}   left unpaid {money(short)}", False]]]]},
        {"title": "It noticed, and chose", "blocks": [
            ["quote", chose.group(0), f"{model}, to the shift log"],
            ["p", "The task had already answered that: nobody gets two."]]},
        {"title": "Its own summary", "blocks": [["quote", headline.group(0), f"{model}, to the shift log"]]},
    ]
    return {"key": "repeat", "model": model, "title": f"how {model.replace('Claude ', '')} failed its first shift",
            "where": f"{COMPANIES[sc['world']]}, large takeover, chat {len(shift['tickets'])} of {len(shift['tickets'])}",
            "rows": len(note["rows"]), "wrong": len(wrong), "missed": len(missed), "steps": steps}


def memo_check(notes_key: str) -> dict | None:
    """Long shifts change two rules mid-shift by memo. For the chats that depend on it: was the memo's new limit in
    the notes the agent had saved by the time the chat began?"""
    path = DASH / "data" / f"{notes_key}__long.json"
    if not path.exists():
        return None
    wrong = missing = total = 0
    for shift in json.loads(path.read_text()).values():
        memo = next((e[2] for t in shift["tickets"] for e in t["events"]
                     if e[0] == "c" and "Policy memo from the shift lead" in (e[2] or "")), None)
        limit = re.search(r"rule 6 is \$(\d+)", memo or "")
        if not limit:
            continue
        saved = [(i, e[2].get("text", "")) for i, t in enumerate(shift["tickets"]) for e in t["events"]
                 if e[0] == "t" and e[1] == "save_notes" and isinstance(e[3], dict) and e[3].get("status") == "saved"]
        for i, t in enumerate(shift["tickets"]):
            if t["kind"] not in ("limit_between_denied", "window_between_denied"):
                continue
            total += 1
            if t["label"] != "correct":
                wrong += 1
                before = [text for j, text in saved if j < i]
                missing += ("$" + limit.group(1)) not in (before[-1] if before else "")
    return {"total": total, "wrong": wrong, "missing": missing}


def memory_pairs(index: dict) -> list:
    """Each limited-memory row beside its full-memory row, on exactly the shifts the limited-memory row was run on."""
    by = {m["key"]: m for m in index["models"]}
    pairs = []
    for m in index["models"]:
        base = by.get(m["key"][:-len("-notes")]) if m["key"].endswith("-notes") else None
        if not base:
            continue
        full = {s["id"]: s["pass"] for s in base["shifts"]}
        both = [s for s in m["shifts"] if s["id"] in full]
        if not both:
            continue
        pairs.append({"name": base["name"], "n": len(both), "full": sum(full[s["id"]] for s in both),
                      "notes": sum(s["pass"] for s in both), "sizes": sorted({s["size"] for s in both}, key=index["sizes"].index),
                      "counts": {size: sum(1 for s in both if s["size"] == size) for size in index["sizes"] if any(s["size"] == size for s in both)},
                      "memo": memo_check(m["key"])})
    return sorted(pairs, key=lambda p: -p["n"])


DOMAINS = {  # the four companies are the benchmark's domains
    "shopright": ["ShopRight", "Online home-goods retail: orders, refunds to card or store credit."],
    "skylark": ["SkyLark Air", "Regional airline: bookings, refunds or travel credit."],
    "clouddesk": ["CloudDesk", "Help-desk software: invoices and credits."],
    "harbor": ["Harbor Bank", "Online bank: statements and fee reversals."],
}
STANDARD = ("short", "long")
SIGNAL = ("long", "takeover")  # the high-signal subset, chosen by shift type


def median(values: list) -> float:
    v = sorted(values)
    return (v[len(v) // 2] + v[(len(v) - 1) // 2]) / 2


def cuts(index: dict, base: list) -> dict:
    """Every cross-cut the page shows, computed from the dashboard index: all shifts, domains, shift types, chat types."""
    worlds = [w for w in DOMAINS if any(s["world"] == w for m in base for s in m["shifts"])]
    total = len({s["id"] for m in index["models"] for s in m["shifts"]})
    per_model = {}
    for m in base:
        std = [s for s in m["shifts"] if s["size"] in STANDARD]
        per_model[m["key"]] = {
            "all": [sum(s["pass"] for s in m["shifts"]), len(m["shifts"])],
            "domains": {w: [sum(s["pass"] for s in std if s["world"] == w), sum(1 for s in std if s["world"] == w)] for w in worlds},
        }
    domain_totals = {w: [sum(per_model[m["key"]]["domains"][w][0] for m in base), sum(per_model[m["key"]]["domains"][w][1] for m in base)] for w in worlds}
    types = []
    for size in index["sizes"]:
        ran = [m for m in base if m["tiers"][size][1] >= 8]
        if not ran:
            continue
        rates = [m["tiers"][size][0] / m["tiers"][size][1] for m in ran]
        best = max(ran, key=lambda m: m["tiers"][size][0] / m["tiers"][size][1])
        high = sum(r >= 0.9 for r in rates)
        read = "saturated" if high * 2 >= len(ran) else "reaches the top" if high == 0 else "separates the middle"
        types.append({"size": size, "models": len(ran), "best": [best["name"], *best["tiers"][size]], "high": high,
                      "median": round(100 * median(rates)), "read": read})
    chats = []
    for kind, info in index["kinds"].items():
        shares = [(m["kinds"][kind][0] / m["kinds"][kind][1], m["name"]) for m in base if m["kinds"].get(kind, [0, 0])[1] >= 6]
        if len(shares) >= 2:
            chats.append({"kind": kind, "family": info["family"], "models": len(shares), "spread": round(100 * (max(shares)[0] - min(shares)[0])),
                          "max": round(100 * max(shares)[0]), "min": round(100 * min(shares)[0]), "low": min(shares)[1]})
    chats.sort(key=lambda c: (-c["spread"], c["kind"]))
    return {"total": total, "worlds": [[w, *DOMAINS[w]] for w in worlds], "models": per_model, "domain_totals": domain_totals,
            "types": types, "chats": chats, "signal": list(SIGNAL)}


def analysis_data(index: dict, base: list) -> dict:
    """What the analysis page needs to pivot and to list every shift by model."""
    ids = sorted({(index["sizes"].index(s["size"]), s["world"], s["seed"], s["id"], s["size"]) for m in base for s in m["shifts"]})
    return {
        "sizes": index["sizes"], "families": index["families"], "kinds": {k: v["family"] for k, v in index["kinds"].items()},
        "worlds": {w: DOMAINS[w][0] for w in DOMAINS},
        "shifts": [{"id": i[3], "world": i[1], "size": i[4], "seed": i[2].replace("s", "")} for i in ids],
        "models": [{"key": m["key"], "name": m["name"], "provider": m["provider"], "main": m["main"], "families": m["families"], "kinds": m["kinds"],
                    "shifts": {s["id"]: [int(s["pass"]), s["correct"], s["total"]] for s in m["shifts"]}} for m in base],
    }


def main() -> None:
    index = dashboard_index()
    pareto_path = ROOT / "results" / "final" / "pareto.json"
    pareto = json.loads(pareto_path.read_text()) if pareto_path.exists() else {}
    models = []
    for m in index["models"]:
        if m["key"].endswith("-notes"):
            continue
        p = pareto.get(m["key"], {})
        order = sorted(m["shifts"], key=lambda s: (index["sizes"].index(s["size"]), s["world"], s["seed"]))
        models.append({k: m[k] for k in ("key", "name", "provider", "tiers", "main", "tickets", "finished")}
                      | {"cost": p.get("cost_shift"), "minutes": p.get("min_shift"), "style": m.get("style", {}),
                         "marks": {size: [int(s["pass"]) for s in order if s["size"] == size] for size in index["sizes"]},
                         "small_batch": m["kinds"].get("batch_recovery_small"),
                         "fails": [[x["world"], x["size"]] for x in order if not x["pass"]]})
    shift_ids = {s["id"] for m in index["models"] for s in m["shifts"]}
    totals = {"models": len(models), "shifts": len(shift_ids), "companies": len({s.split("__")[0] for s in shift_ids}),
              "types": len(index["sizes"]), "chats": sum(f[1] for m in index["models"] for f in m["families"].values())}
    base = [m for m in index["models"] if not m["key"].endswith("-notes")]
    data = {"built": datetime.date.today().isoformat(), "totals": totals, "models": models, "sizes": index["sizes"], "cuts": cuts(index, base),
            "style": index.get("style", []), "memory": memory_pairs(index), "game": game(),
            "replays": [r for r in (replay_repeat(), replay()) if r]}
    body = (ROOT / "scripts" / "site_template.html").read_text().replace(
        "/*DATA*/null", json.dumps(data, separators=(",", ":"), ensure_ascii=False).replace("</", "<\\/"))
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "page.html").write_text(body)
    (OUT / "index.html").write_text(
        '<!doctype html>\n<html lang="en">\n<head>\n<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n<style>body{margin:0}</style>\n</head>\n<body>\n'
        + body + "\n</body>\n</html>\n")
    page = (ROOT / "scripts" / "site_analysis_template.html")
    if page.exists():
        blob = json.dumps({"built": data["built"], **analysis_data(index, base)}, separators=(",", ":"), ensure_ascii=False).replace("</", "<\\/")
        (OUT / "analysis.html").write_text(page.read_text().replace("/*DATA*/null", blob))
    c = data["cuts"]
    print("  domains:", {w: c["domain_totals"][w] for w in c["domain_totals"]})
    print("  types:", [(t["size"], t["models"], t["best"][1:], t["high"], t["median"], t["read"]) for t in c["types"]])
    print("  chat signal top:", [(x["kind"], x["spread"]) for x in c["chats"][:6]], "bottom:", [(x["kind"], x["spread"]) for x in c["chats"][-6:]])
    print("  all:", {k: v["all"] for k, v in c["models"].items()})
    print("  long:", [(m["name"], m["tiers"]["long"], m["tiers"].get("takeover")) for m in sorted(base, key=lambda m: -m["tiers"]["long"][0])])
    print(f"wrote {OUT}: {totals}; game {'yes' if data['game'] else 'MISSING'}, replay "
          f"{[(r['key'], len(r['steps'])) for r in data['replays']]}, page {len(body) / 1e3:.0f} KB")
    for pair in data["memory"]:
        print(f"  limited memory: {pair['name']} {pair['full']} -> {pair['notes']} of {pair['n']} {pair['sizes']} memo {pair['memo']}")
    if data["game"]:
        print("  game rows:", [(r["n"], r["kind"], r["owed"]) for r in data["game"]["rows"]])


if __name__ == "__main__":
    main()
