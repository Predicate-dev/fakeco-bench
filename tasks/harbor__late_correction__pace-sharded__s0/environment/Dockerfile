FROM python:3.12-slim-bookworm

RUN apt-get update \
 && apt-get install -y --no-install-recommends sudo curl ca-certificates git procps \
 && rm -rf /var/lib/apt/lists/*

# For the verifier's optional LLM judge (tests/judge.py). The agent has no API key in this sandbox.
RUN pip install --no-cache-dir --quiet "anthropic>=0.71,<2"

# `agent` is the account the evaluated agent runs as. `fakeco` owns the company's
# back office: the scenario, the ledger and the chat log. The agent reaches it only
# through `desk`, so it cannot read upcoming customer turns or edit what gets graded.
RUN useradd --create-home --shell /bin/bash agent \
 && useradd --system --home-dir /opt/fakeco --shell /usr/sbin/nologin fakeco

COPY rt/ /opt/fakeco/rt/

RUN printf '#!/usr/local/bin/python3 -I\nimport sys\nsys.path.insert(0, "/opt/fakeco/rt")\nimport desk\nsys.exit(desk.main(sys.argv[1:]))\n' > /opt/fakeco/rt/desk \
 && chmod 700 /opt/fakeco/rt/desk \
 && mkdir -p /opt/fakeco/state \
 && chown -R fakeco:fakeco /opt/fakeco \
 && chmod -R go-rwx /opt/fakeco \
 && printf 'ALL ALL=(fakeco) NOPASSWD: /opt/fakeco/rt/desk\n' > /etc/sudoers.d/fakeco \
 && chmod 440 /etc/sudoers.d/fakeco \
 && printf '#!/bin/sh\nexec sudo -n -u fakeco /opt/fakeco/rt/desk "$@"\n' > /usr/local/bin/desk \
 && chmod 755 /usr/local/bin/desk \
 && mkdir -p /workspace /logs \
 && chown agent:agent /workspace \
 && chmod 777 /logs \
 && chmod 755 /root

WORKDIR /workspace
