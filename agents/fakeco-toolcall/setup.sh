#!/bin/bash
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive

if ! command -v python3 > /dev/null; then
  apt-get update && apt-get install -y python3 python3-venv
fi

python3 -m venv /opt/fakeco-toolcall/venv
/opt/fakeco-toolcall/venv/bin/pip install --quiet --no-cache-dir "anthropic>=0.71,<2" "openai>=1.60,<3"

# The task may run the agent as an unprivileged account.
chmod -R a+rX /opt/fakeco-toolcall /bundle/fakeco-toolcall
