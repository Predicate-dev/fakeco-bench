"""Check generated task folders without Docker or a model.

For every task, play the scripted oracle and naive agents through the task's own
desk runtime (environment/rt) and grade with the task's own verifier
(tests/score.py). Each verdict must equal what the original in-process FakeCo
harness gives the same agent on the same scenario, the oracle must pass every
task, and the naive agent must fail every non-control task.

    python3 harbor/validate.py [tasks]

This exercises the task files, not the sandbox: file permissions, sudo and the
image build are only checked by running a task in a container.
"""
from __future__ import annotations
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "harbor"))

import driver  # noqa: E402
from fakeco.agents import SCRIPTED  # noqa: E402
from fakeco.core import run_episode  # noqa: E402
from fakeco.scenarios import Scenario  # noqa: E402

SHIM = ("import sys; from pathlib import Path; sys.path.insert(0, sys.argv[1]); import desk; "
        "sys.exit(desk.main(sys.argv[3:], home=Path(sys.argv[2])))")


def play(task: Path, agent: str) -> dict:
    scenario = json.loads((task / "tests" / "scenario.json").read_text())
    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp)
        shutil.copytree(task / "environment" / "rt", home / "rt")

        def desk(argv):
            return subprocess.run([sys.executable, "-I", "-B", "-c", SHIM, str(home / "rt"), str(home), *argv],
                                  capture_output=True, text=True, check=True).stdout

        driver.run(scenario, agent, desk)
        subprocess.run([sys.executable, "-I", "-B", str(task / "tests" / "score.py"), "--state", str(home / "state"),
                        "--out", str(home / "out"), "--scenario", str(task / "tests" / "scenario.json")],
                       capture_output=True, text=True, check=True)
        verdict = json.loads((home / "out" / "score.json").read_text())
        verdict["reward"] = json.loads((home / "out" / "reward.json").read_text())["reward"]
        return verdict


def reference(task: Path, agent: str) -> dict:
    sc = Scenario(**json.loads((task / "tests" / "scenario.json").read_text()))
    return run_episode(SCRIPTED[agent](sc), sc)["score"]


def main() -> int:
    tasks_dir = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "tasks"
    index = {row["task_id"]: row for row in json.loads((tasks_dir.parent / f"{tasks_dir.name}-index.json").read_text())}
    problems, counts = [], {"oracle": [0, 0], "naive": [0, 0]}
    real_print, driver.print = print, lambda *a, **k: None  # the driver narrates each turn; not wanted here
    for task in sorted(p for p in tasks_dir.iterdir() if (p / "task.toml").is_file()):
        runtime_scenario = json.loads((task / "environment" / "rt" / "scenario.json").read_text())
        leaked = {"meta", "dials", "mode"} & set(runtime_scenario)
        if leaked:
            problems.append(f"{task.name}: the agent-side scenario carries answer-key fields {sorted(leaked)}")
        for agent in ("oracle", "naive"):
            got, want = play(task, agent), reference(task, agent)
            counts[agent][0] += got["score"]["pass"]
            counts[agent][1] += 1
            if (got["score"]["pass"], got["score"]["label"]) != (want["pass"], want["label"]):
                problems.append(f"{task.name}: {agent} scored {got['score']} in the task, {want} in the harness")
            if got["reward"] != float(got["score"]["pass"]):
                problems.append(f"{task.name}: reward {got['reward']} disagrees with pass={got['score']['pass']}")
            if agent == "oracle" and not got["score"]["pass"]:
                problems.append(f"{task.name}: the oracle failed ({got['score']['label']})")
            if agent == "naive" and got["score"]["pass"] and not index[task.name]["control"]:
                problems.append(f"{task.name}: the naive agent passed a non-control task")
    for agent, (passed, total) in counts.items():
        real_print(f"{agent}: {passed}/{total} pass")
    for line in problems:
        real_print("PROBLEM", line)
    real_print("OK" if not problems else f"{len(problems)} problem(s)")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
