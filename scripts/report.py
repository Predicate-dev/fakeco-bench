"""Summarize Valkyrie results for FakeCo runs as Markdown tables, and optionally a chart.

    valkyrie run results RUN_ID --path results/sonnet.json
    python3 scripts/report.py results/sonnet.json results/gpt.json [--chart results/fakeco.png]

Each file is one run. The label for a run is its file name without the extension.
Per-task verdicts come from the FAKECO_SCORE line the verifier prints; world, mode
and dials come from the task ID via tasks-index.json.
"""
from __future__ import annotations
import argparse
import json
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MODES = ["mutant_policy", "silent_tool_failure", "stale_document", "late_correction", "takeover"]
WORLDS = ["shopright", "skylark", "clouddesk", "harbor"]


def find_tasks(node, index: dict, found: dict) -> None:
    """Results files nest per-task entries under run metadata; pick out every dict keyed by a known task ID."""
    if isinstance(node, dict):
        for key, value in node.items():
            if key in index and isinstance(value, dict):
                found.setdefault(key, value)
            else:
                find_tasks(value, index, found)
    elif isinstance(node, list):
        for value in node:
            find_tasks(value, index, found)


def verdict(entry: dict) -> dict:
    """{'pass': bool|None, 'label': str} for one task entry; pass is None when the task was never graded."""
    text = json.dumps(entry)
    marker = "FAKECO_SCORE "
    if marker in text:
        raw = text.rsplit(marker, 1)[1]
        # The line sits inside a JSON string, so its quotes are escaped once.
        line = raw.split("\\n", 1)[0].replace('\\"', '"').replace("\\\\", "\\")
        try:
            score = json.JSONDecoder().raw_decode(line)[0]["score"]
            return {"pass": bool(score["pass"]), "label": score["label"], "graded_by": score.get("graded_by", ""),
                    "tickets": score.get("tickets")}
        except (ValueError, KeyError):
            pass
    rewards = entry.get("rewards") or (entry.get("verifier_result") or {}).get("rewards") or {}
    if "reward" in rewards:
        return {"pass": rewards["reward"] >= 1, "label": "pass" if rewards["reward"] >= 1 else "fail", "graded_by": ""}
    return {"pass": None, "label": "not_graded", "graded_by": ""}


def load(path: Path, index: dict) -> list[dict]:
    found: dict = {}
    find_tasks(json.loads(path.read_text()), index, found)
    rows = {tid: {**index[tid], **verdict(entry)} for tid, entry in sorted(found.items())}
    judged = path.with_suffix(".judged.json")  # written by scripts/judge_results.py
    if judged.exists():
        for tid, score in json.loads(judged.read_text()).items():
            if tid in rows:
                rows[tid].update({"pass": bool(score["pass"]), "label": score["label"],
                                  "graded_by": score.get("graded_by", "")})
    return list(rows.values())


def rate(rows: list[dict]) -> str:
    graded = [r for r in rows if r["pass"] is not None]
    if not rows:
        return "-"
    return f"{sum(r['pass'] for r in graded)}/{len(rows)}"


def dial_tag(row: dict) -> str:
    return ", ".join(f"{k}={v}" for k, v in row["dials"].items())


def report_shifts(label: str, rows: list[dict]) -> str:
    """Hard tier: a shift passes only if every ticket in it is right, so show both levels."""
    graded = [r for r in rows if r.get("tickets")]
    tickets = [t for r in graded for t in r["tickets"]]
    right = sum(t["label"] == "correct" for t in tickets)
    out = [f"## {label} (hard tier: shifts)", "",
           f"Shifts with every ticket right: **{sum(bool(r['pass']) for r in rows)}/{len(rows)}**. "
           f"Tickets right: **{right}/{len(tickets)}** ({100 * right / max(len(tickets), 1):.0f}%).", "",
           "| Shift size | " + " | ".join(WORLDS) + " | all |", "|---|" + "---|" * (len(WORLDS) + 1)]
    for size in sorted({r["dials"]["size"] for r in rows}, reverse=True):
        cell = [r for r in rows if r["dials"]["size"] == size]
        out.append(f"| {size} | " + " | ".join(rate([r for r in cell if r["world"] == w]) for w in WORLDS)
                   + f" | {rate(cell)} |")
    kinds = defaultdict(list)
    for t in tickets:
        kinds[t["kind"]].append(t["label"])
    out += ["", "| Ticket type | Right | What went wrong |", "|---|---|---|"]
    for kind, labels in sorted(kinds.items(), key=lambda kv: sum(l == "correct" for l in kv[1]) / len(kv[1])):
        wrong = Counter(l for l in labels if l != "correct")
        out.append(f"| `{kind}` | {sum(l == 'correct' for l in labels)}/{len(labels)} | "
                   + (", ".join(f"{k} ×{v}" for k, v in wrong.most_common()) or "") + " |")
    return "\n".join(out)


def report(label: str, rows: list[dict]) -> str:
    if rows and all(r["mode"] == "shift" for r in rows):
        return report_shifts(label, rows)
    out = [f"## {label}", ""]
    tests = [r for r in rows if not r["control"]]
    controls = [r for r in rows if r["control"]]
    ungraded = sum(r["pass"] is None for r in rows)
    out.append(f"Test conditions passed: **{rate(tests)}**. Controls passed: **{rate(controls)}**."
               + (f" Not graded (errors): {ungraded}." if ungraded else ""))
    out += ["", "| Failure mode | " + " | ".join(WORLDS) + " | all |", "|---|" + "---|" * (len(WORLDS) + 1)]
    for mode in MODES:
        cells = [rate([r for r in tests if r["mode"] == mode and r["world"] == w]) for w in WORLDS]
        out.append(f"| `{mode}` | " + " | ".join(cells) + f" | {rate([r for r in tests if r['mode'] == mode])} |")
    out.append("| controls | " + " | ".join(rate([r for r in controls if r["world"] == w]) for w in WORLDS)
               + f" | {rate(controls)} |")
    for mode in MODES:
        groups = defaultdict(list)
        for r in rows:
            if r["mode"] == mode:
                groups[dial_tag(r)].append(r)
        if not groups:
            continue
        out += ["", f"**`{mode}` by dial**", "", "| Dials | Passed | Outcomes |", "|---|---|---|"]
        for tag, group in groups.items():
            labels = Counter(r["label"] for r in group)
            control = " (control)" if group[0]["control"] else ""
            out.append(f"| {tag}{control} | {rate(group)} | "
                       + ", ".join(f"{k} ×{v}" for k, v in labels.most_common()) + " |")
    return "\n".join(out)


def chart(runs: dict[str, list[dict]], path: Path) -> None:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    fig, ax = plt.subplots(figsize=(9, 4.5))
    width = 0.8 / max(len(runs), 1)
    for i, (label, rows) in enumerate(runs.items()):
        tests = [r for r in rows if not r["control"]]
        rates = []
        for mode in MODES:
            cell = [r for r in tests if r["mode"] == mode]
            rates.append(100 * sum(bool(r["pass"]) for r in cell) / len(cell) if cell else 0)
        xs = [x + i * width for x in range(len(MODES))]
        bars = ax.bar(xs, rates, width, label=label)
        ax.bar_label(bars, fmt="%.0f%%", fontsize=8, padding=2)
    ax.set_xticks([x + width * (len(runs) - 1) / 2 for x in range(len(MODES))])
    ax.set_xticklabels([m.replace("_", " ") for m in MODES])
    ax.set_ylim(0, 110)
    ax.set_ylabel("Test conditions passed (%)")
    ax.set_title("FakeCo: pass rate by failure mode (controls excluded)")
    ax.spines[["top", "right"]].set_visible(False)
    ax.legend(frameon=False)
    fig.tight_layout()
    fig.savefig(path, dpi=160)


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("files", nargs="+", type=Path)
    p.add_argument("--index", type=Path, nargs="*", default=[ROOT / "tasks-index.json", ROOT / "tasks-hard-index.json"])
    p.add_argument("--chart", type=Path, default=None, help="also write a bar chart (needs matplotlib)")
    args = p.parse_args()

    index = {row["task_id"]: row for path in args.index if path.exists() for row in json.loads(path.read_text())}
    runs = {path.stem: load(path, index) for path in args.files}
    print("\n\n".join(report(label, rows) for label, rows in runs.items()))
    if args.chart:
        chart(runs, args.chart)


if __name__ == "__main__":
    main()
