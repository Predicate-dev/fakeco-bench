#!/bin/bash
# Reference solution: FakeCo's scripted oracle, which reads the answer key.
set -euo pipefail
python3 -I /solution/driver.py --agent oracle
