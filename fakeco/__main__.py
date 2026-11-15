"""python -m fakeco {run,report,show} ...

  run     run a suite against an agent, write JSONL
  report  summarize one or more JSONL result files
  show    print a scenario (system prompt, world state, user turns)
"""
from __future__ import annotations
import argparse
import json
import sys
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor

from .agents import SCRIPTED, AnthropicAgent, OpenAICompatAgent
from .core import run_episode
from .scenarios import DIAL_GRID, MODES, build_suite, make_scenario
from .worlds import WORLDS


def _agent_factory(args):
    if args.agent in SCRIPTED:
        return lambda sc: SCRIPTED[args.agent](sc)
    if args.agent == "anthropic":
        return lambda sc: AnthropicAgent(args.model, temperature=args.temperature)
    if args.agent == "openai":
        return lambda sc: OpenAICompatAgent(args.model, base_url=args.base_url, temperature=args.temperature)
    sys.exit(f"unknown agent {args.agent}")


def cmd_run(args):
    suite = build_suite(args.worlds, args.modes, n=args.n, seed=args.seed)
    make = _agent_factory(args)
    label = args.label or (args.model or args.agent)
    print(f"Running {len(suite)} scenarios with {label} ...", file=sys.stderr)

    def one(sc):
        r = run_episode(make(sc), sc)
        r["agent"] = label
        return r

    with ThreadPoolExecutor(max_workers=args.workers) as pool, open(args.out, "w") as f:
        for k, r in enumerate(pool.map(one, suite), 1):
            f.write(json.dumps(r) + "\n")
            mark = "PASS" if r["score"]["pass"] else "FAIL"
            print(f"[{k:>3}/{len(suite)}] {mark}  {r['id']}  ({r['score']['label']})", file=sys.stderr)
    print(f"Wrote {args.out}", file=sys.stderr)


def _rate(rows):
    return f"{sum(r['score']['pass'] for r in rows)}/{len(rows)}"


def cmd_report(args):
    rows = [json.loads(l) for path in args.files for l in open(path) if l.strip()]
    agents = sorted({r["agent"] for r in rows})
    for agent in agents:
        rs = [r for r in rows if r["agent"] == agent]
        print(f"\n=== {agent}  overall {_rate(rs)} ===")
        print(f"\n{'pass rate':<22}" + "".join(f"{w:>12}" for w in WORLDS))
        for m in MODES:
            line = f"{m:<22}"
            for w in WORLDS:
                cell = [r for r in rs if r["mode"] == m and r["world"] == w]
                line += f"{_rate(cell) if cell else '-':>12}"
            print(line)
        for m in MODES:
            cell = [r for r in rs if r["mode"] == m]
            if not cell:
                continue
            print(f"\n-- {m}: by dials --")
            groups = defaultdict(list)
            for r in cell:
                groups[json.dumps(r["dials"], sort_keys=True)].append(r)
            for key in sorted(groups):
                g = groups[key]
                labels = Counter(r["score"]["label"] for r in g)
                print(f"  {key:<70} {_rate(g):>6}   " + ", ".join(f"{k}:{v}" for k, v in labels.most_common()))


def cmd_show(args):
    dials = DIAL_GRID[args.mode][args.dial_index]
    sc = make_scenario(args.world, args.mode, dials, args.seed)
    print(f"# {sc.id}\n\n## System\n{sc.system}\n\n## Faults\n{sc.faults or 'none'}\n")
    if sc.preface:
        print(f"## Internal note shown before the first customer message\n{sc.preface}\n")
    print("## World state\n" + json.dumps(sc.db, indent=2) + "\n\n## User turns")
    for i, t in enumerate(sc.user_turns):
        print(f"{i}. {t}")


def main():
    p = argparse.ArgumentParser(prog="fakeco")
    sub = p.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("run")
    r.add_argument("--agent", default="anthropic", help="anthropic | openai | naive | oracle")
    r.add_argument("--model", default=None)
    r.add_argument("--base-url", default=None, help="for --agent openai with a non-OpenAI endpoint")
    r.add_argument("--label", default=None, help="name for this agent in reports")
    r.add_argument("--worlds", nargs="*", choices=list(WORLDS), default=None)
    r.add_argument("--modes", nargs="*", choices=MODES, default=None)
    r.add_argument("--n", type=int, default=1, help="seeds per (world, mode, dial) cell")
    r.add_argument("--seed", type=int, default=0)
    r.add_argument("--temperature", type=float, default=0.0)
    r.add_argument("--workers", type=int, default=4)
    r.add_argument("--out", default="results.jsonl")
    r.set_defaults(fn=cmd_run)

    rp = sub.add_parser("report")
    rp.add_argument("files", nargs="+")
    rp.set_defaults(fn=cmd_report)

    s = sub.add_parser("show")
    s.add_argument("--world", choices=list(WORLDS), required=True)
    s.add_argument("--mode", choices=MODES, required=True)
    s.add_argument("--dial-index", type=int, default=0)
    s.add_argument("--seed", type=int, default=0)
    s.set_defaults(fn=cmd_show)

    args = p.parse_args()
    if getattr(args, "agent", None) in ("anthropic", "openai") and not args.model:
        sys.exit("--model is required for anthropic/openai agents")
    args.fn(args)


if __name__ == "__main__":
    main()
