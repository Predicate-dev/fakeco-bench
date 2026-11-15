#!/bin/bash
# Grades from the desk's state files, which the agent account cannot read or write.
set -uo pipefail
mkdir -p /logs/verifier
python3 -I /tests/score.py --state /opt/fakeco/state --out /logs/verifier
status=$?
if [ ! -s /logs/verifier/reward.json ]; then
  echo "score.py exited with $status and wrote no reward" >&2
  exit 1
fi
