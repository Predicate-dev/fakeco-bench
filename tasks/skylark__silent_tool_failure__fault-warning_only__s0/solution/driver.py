"""Drive the desk with one of FakeCo's scripted agents.

`oracle` is the reference solution shipped in each task's solution/ folder;
`naive` is the negative control used when validating tasks. Both read the answer
key, so neither is a model baseline.

    python3 -I driver.py --agent oracle [--scenario scenario.json]
"""
from __future__ import annotations
import argparse
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from fakeco.agents import SCRIPTED  # noqa: E402
from fakeco.core import shown_text  # noqa: E402


def desk_cli(argv: list[str]) -> str:
    return subprocess.run(["desk", *argv], capture_output=True, text=True, check=False).stdout


def run(scenario: dict, agent_name: str, desk=desk_cli) -> None:
    sc = SimpleNamespace(**scenario)
    agent = SCRIPTED[agent_name](sc)
    agent.reset(sc.system, [])

    def call(name: str, args: dict) -> dict:
        return json.loads(desk(["call", name, json.dumps(args)]))

    state = json.loads(desk(["inbox", "--json"]))
    while not state["closed"]:
        print(f"[customer] {state['customer']}")
        reply = agent.respond(shown_text({"text": state["customer"], "note": state.get("internal_note")}), call)
        print(f"[agent] {reply}")
        state = json.loads(desk(["reply", "--json", reply]))


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--agent", choices=sorted(SCRIPTED), default="oracle")
    p.add_argument("--scenario", type=Path, default=HERE / "scenario.json")
    a = p.parse_args()
    run(json.loads(a.scenario.read_text()), a.agent)
