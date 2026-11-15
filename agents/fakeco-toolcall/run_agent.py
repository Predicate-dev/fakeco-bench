"""fakeco-toolcall: a bare function-calling support agent for FakeCo tasks.

The model gets the company brief as its system prompt, the back-office tools as
native tools, and the customer's messages as user turns. Tool calls and replies
go through the sandbox's `desk` command, so the same task and verifier serve
this harness and shell-driving agents such as Claude Code.

    run_agent.py INSTRUCTION_PATH --task-id ID --model provider/model
"""
from __future__ import annotations
import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

# The task instruction ends with a section on driving `desk` from a shell. This
# harness exposes the tools natively, so the model is shown only what precedes it.
DESK_SECTION = "\n## Your support desk"
NATIVE_NOTE = ("\n\nYou are in a live chat with a customer. Each message you write is sent to the customer; "
               "use your tools for everything else.")
EFFORTS = ("low", "medium", "high", "xhigh", "max")


def desk(*argv: str, stdin: str | None = None) -> str:
    return subprocess.run(["desk", *argv], input=stdin, capture_output=True, text=True, check=False).stdout


def call_tool(name: str, args: dict) -> str:
    return desk("call", name, "-", stdin=json.dumps(args))


class Trace:
    """Everything the model saw and did, one JSON object per line, for reading rollouts."""

    def __init__(self, out: Path):
        out.mkdir(parents=True, exist_ok=True)
        self.path = out / "trajectory.jsonl"
        self.path.write_text("")
        self.usage = {"input_tokens": 0, "output_tokens": 0, "requests": 0}

    def add(self, kind: str, **row) -> None:
        with self.path.open("a") as f:
            f.write(json.dumps({"kind": kind, "ts": time.time(), **row}, default=str) + "\n")

    def count(self, input_tokens: int, output_tokens: int) -> None:
        self.usage["input_tokens"] += input_tokens or 0
        self.usage["output_tokens"] += output_tokens or 0
        self.usage["requests"] += 1


class AnthropicChat:
    def __init__(self, model: str, system: str, tools: list[dict], max_steps: int, effort: str, trace: Trace):
        import anthropic
        self.client = anthropic.Anthropic(max_retries=6)
        self.model, self.system, self.tools = model, system, tools
        self.max_steps, self.trace, self.messages = max_steps, trace, []
        # Sampling parameters are left at the API defaults: current models reject overrides.
        self.extra = {"output_config": {"effort": effort}} if effort in EFFORTS else {}

    def respond(self, user_text: str) -> str:
        self.messages.append({"role": "user", "content": user_text})
        texts = []
        for _ in range(self.max_steps):
            # Top-level cache_control caches the conversation so far; a shift re-sends it on every call.
            resp = self.client.messages.create(model=self.model, max_tokens=8000, system=self.system,
                                               tools=self.tools, messages=self.messages,
                                               cache_control={"type": "ephemeral"}, **self.extra)
            usage = resp.usage
            self.trace.count(usage.input_tokens + (getattr(usage, "cache_read_input_tokens", 0) or 0)
                             + (getattr(usage, "cache_creation_input_tokens", 0) or 0), usage.output_tokens)
            self.trace.usage["cache_read_input_tokens"] = (self.trace.usage.get("cache_read_input_tokens", 0)
                                                           + (getattr(usage, "cache_read_input_tokens", 0) or 0))
            self.trace.add("model", stop_reason=resp.stop_reason, content=[b.model_dump() for b in resp.content])
            # Hand the content back unchanged so thinking blocks stay attached to their tool calls.
            self.messages.append({"role": "assistant", "content": resp.content})
            texts += [b.text for b in resp.content if b.type == "text" and b.text.strip()]
            uses = [b for b in resp.content if b.type == "tool_use"]
            if resp.stop_reason != "tool_use" or not uses:
                break
            results = []
            for u in uses:
                out = call_tool(u.name, u.input)
                self.trace.add("tool", name=u.name, args=u.input, result=out)
                results.append({"type": "tool_result", "tool_use_id": u.id, "content": out})
            self.messages.append({"role": "user", "content": results})
        return "\n".join(texts).strip()


class OpenAIChat:
    def __init__(self, model: str, system: str, tools: list[dict], max_steps: int, effort: str, trace: Trace):
        from openai import OpenAI
        self.client = OpenAI(max_retries=6)
        self.model, self.max_steps, self.trace = model, max_steps, trace
        self.tools = [{"type": "function", "function": {
            "name": t["name"], "description": t["description"], "parameters": t["input_schema"]}} for t in tools]
        self.messages = [{"role": "system", "content": system}]

    def respond(self, user_text: str) -> str:
        self.messages.append({"role": "user", "content": user_text})
        texts = []
        for _ in range(self.max_steps):
            resp = self.client.chat.completions.create(model=self.model, messages=self.messages, tools=self.tools)
            msg = resp.choices[0].message
            if resp.usage:
                self.trace.count(resp.usage.prompt_tokens, resp.usage.completion_tokens)
            self.trace.add("model", stop_reason=resp.choices[0].finish_reason, content=msg.model_dump(exclude_none=True))
            self.messages.append(msg.model_dump(exclude_none=True))
            if msg.content and msg.content.strip():
                texts.append(msg.content)
            if not msg.tool_calls:
                break
            for tc in msg.tool_calls:
                try:
                    args = json.loads(tc.function.arguments or "{}")
                except json.JSONDecodeError:
                    args = {}
                out = call_tool(tc.function.name, args)
                self.trace.add("tool", name=tc.function.name, args=args, result=out)
                self.messages.append({"role": "tool", "tool_call_id": tc.id, "content": out})
        return "\n".join(texts).strip()


PROVIDERS = {"anthropic": AnthropicChat, "openai": OpenAIChat}


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("instruction_path", type=Path)
    p.add_argument("--task-id", default="")
    p.add_argument("--model", required=True)
    p.add_argument("--max-steps", type=int, default=100)
    p.add_argument("--effort", default="default")
    p.add_argument("--out", type=Path, default=Path(os.environ.get("AGENT_OUTPUT_ROOT", "/logs/agent")))
    args = p.parse_args()

    provider, _, model = args.model.partition("/")
    if provider not in PROVIDERS or not model:
        raise SystemExit(f"--model must look like anthropic/<model> or openai/<model>, got {args.model!r}")

    brief = args.instruction_path.read_text().split(DESK_SECTION)[0]
    # Valkyrie strips the canary comment before upload; drop it here too for runs straight from a task folder.
    brief = "\n".join(line for line in brief.splitlines() if "canary" not in line.lower() or not line.startswith("<!--"))
    system = brief.strip() + NATIVE_NOTE
    tools = json.loads(desk("tools"))
    trace = Trace(args.out)
    trace.add("start", task_id=args.task_id, model=args.model, system=system, tools=[t["name"] for t in tools])
    chat = PROVIDERS[provider](model, system, tools, args.max_steps, args.effort, trace)

    state = json.loads(desk("inbox", "--json"))
    while not state["closed"]:
        trace.add("customer", turn=state["turn"], text=state["customer"], internal_note=state.get("internal_note"))
        shown = state["customer"]
        if state.get("internal_note"):  # e.g. a colleague's handoff note or a memo; the customer never sees it
            shown = f"[Internal note, not visible to the customer]\n{state['internal_note']}\n\n[Customer]\n{shown}"
        reply = chat.respond(shown) or "[The agent sent no message.]"
        trace.add("reply", turn=state["turn"], text=reply)
        state = json.loads(desk("reply", "--json", "-", stdin=reply))

    (args.out / "summary.json").write_text(json.dumps(
        {"task_id": args.task_id, "model": args.model, "agent": "fakeco-toolcall", **trace.usage}, indent=2))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:  # surface the reason in the task's error instead of a bare exit code
        error_path = os.environ.get("VALKYRIE_ERROR_PATH")
        if error_path:
            Path(error_path).write_text(f"{type(e).__name__}: {str(e)[:500]}\n")
        raise
