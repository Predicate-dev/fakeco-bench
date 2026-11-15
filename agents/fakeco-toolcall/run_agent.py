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

# --hint: an optional extra line of instruction, for testing whether one sentence removes a failure.
HINTS = {"none": "", "group": "\n\nWhen you work through a list, group it by customer before you issue anything."}

# --memory notes: the conversation is dropped at the start of every chat. Only the agent's own notes carry over.
MEMORY_NOTE = (
    "\n\nMEMORY. You do not keep the conversation between chats. Each chat starts fresh: you will not see earlier "
    "chats, their tool results or their internal notes. The only thing carried over is your shift notes, which "
    "are shown at the start of every chat. Save them with save_notes, which replaces the whole text (at most "
    "{limit} characters). Anything a later chat may depend on has to be in your notes: memos and notices, running "
    "totals, anything you will need again.")
NOTES_TOOL = {
    "name": "save_notes",
    "description": "Replace your shift notes with this text. The notes are shown to you at the start of every "
                   "later chat, and are the only thing you keep from one chat to the next.",
    "input_schema": {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]},
}
NOTES = {"text": "", "limit": 0}  # limit 0: full memory, no notes tool


def desk(*argv: str, stdin: str | None = None) -> str:
    return subprocess.run(["desk", *argv], input=stdin, capture_output=True, text=True, check=False).stdout


def call_tool(name: str, args: dict) -> str:
    if name == "save_notes" and NOTES["limit"]:  # the harness's own tool; it never reaches the company's desk
        text = str((args or {}).get("text", ""))
        if len(text) > NOTES["limit"]:
            return json.dumps({"error": f"Notes are limited to {NOTES['limit']} characters and this was "
                                        f"{len(text)}. Nothing was saved."})
        NOTES["text"] = text
        return json.dumps({"status": "saved", "characters": len(text)})
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

    def reset(self) -> None:
        self.messages = []

    def respond(self, user_text: str) -> str:
        self.messages.append({"role": "user", "content": user_text})
        texts = []
        for _ in range(self.max_steps):
            # Top-level cache_control caches the conversation so far; a shift re-sends it on every call.
            # Streamed, so a long response (dozens of parallel tool calls in a large batch) is not cut short
            # by a request timeout; the final message is assembled by the SDK.
            with self.client.messages.stream(model=self.model, max_tokens=32000, system=self.system,
                                             tools=self.tools, messages=self.messages,
                                             cache_control={"type": "ephemeral"}, **self.extra) as stream:
                resp = stream.get_final_message()
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
            if not uses:  # every tool call gets its result, even when the response stopped at the token limit
                break
            results = []
            for u in uses:
                out = call_tool(u.name, u.input)
                self.trace.add("tool", name=u.name, args=u.input, result=out)
                results.append({"type": "tool_result", "tool_use_id": u.id, "content": out})
            self.messages.append({"role": "user", "content": results})
        return "\n".join(texts).strip()


class OpenAIChat:
    """OpenAI Responses API: current reasoning models only accept function tools there."""

    def __init__(self, model: str, system: str, tools: list[dict], max_steps: int, effort: str, trace: Trace):
        from openai import OpenAI
        self.client = OpenAI(max_retries=6)
        self.model, self.system, self.max_steps, self.trace = model, system, max_steps, trace
        self.tools = [{"type": "function", "name": t["name"], "description": t["description"],
                       "parameters": t["input_schema"], "strict": False} for t in tools]
        self.previous = None  # the conversation, reasoning included, is kept server-side between calls

    def reset(self) -> None:
        self.previous = None

    def respond(self, user_text: str) -> str:
        pending = [{"role": "user", "content": user_text}]
        texts = []
        for _ in range(self.max_steps):
            resp = self.client.responses.create(model=self.model, instructions=self.system, input=pending,
                                                tools=self.tools, previous_response_id=self.previous)
            self.previous = resp.id
            if resp.usage:
                self.trace.count(resp.usage.input_tokens, resp.usage.output_tokens)
                cached = getattr(getattr(resp.usage, "input_tokens_details", None), "cached_tokens", 0) or 0
                self.trace.usage["cache_read_input_tokens"] = self.trace.usage.get("cache_read_input_tokens", 0) + cached
            self.trace.add("model", stop_reason=resp.status, content=[i.model_dump(exclude_none=True) for i in resp.output])
            if (resp.output_text or "").strip():
                texts.append(resp.output_text)
            calls = [i for i in resp.output if i.type == "function_call"]
            if not calls:
                break
            pending = []
            for c in calls:
                try:
                    args = json.loads(c.arguments or "{}")
                except json.JSONDecodeError:
                    args = {}
                out = call_tool(c.name, args)
                self.trace.add("tool", name=c.name, args=args, result=out)
                pending.append({"type": "function_call_output", "call_id": c.call_id, "output": out})
        return "\n".join(texts).strip()


PROVIDERS = {"anthropic": AnthropicChat, "openai": OpenAIChat}


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("instruction_path", type=Path)
    p.add_argument("--task-id", default="")
    p.add_argument("--model", required=True)
    p.add_argument("--max-steps", type=int, default=100)
    p.add_argument("--effort", default="default")
    p.add_argument("--memory", default="full", choices=["full", "notes"],
                   help="notes: every chat starts with a fresh context and only the agent's saved notes")
    p.add_argument("--notes-limit", type=int, default=1200, help="characters the notes may hold (--memory notes)")
    p.add_argument("--hint", default="none", choices=sorted(HINTS), help="extra line of instruction (see HINTS)")
    p.add_argument("--out", type=Path, default=Path(os.environ.get("AGENT_OUTPUT_ROOT", "/logs/agent")))
    args = p.parse_args()

    provider, _, model = args.model.partition("/")
    if provider not in PROVIDERS or not model:
        raise SystemExit(f"--model must look like anthropic/<model> or openai/<model>, got {args.model!r}")

    brief = args.instruction_path.read_text().split(DESK_SECTION)[0]
    # Valkyrie strips the canary comment before upload; drop it here too for runs straight from a task folder.
    brief = "\n".join(line for line in brief.splitlines() if "canary" not in line.lower() or not line.startswith("<!--"))
    system = brief.strip() + NATIVE_NOTE + HINTS[args.hint]
    tools = json.loads(desk("tools"))
    if args.memory == "notes":
        NOTES["limit"] = args.notes_limit
        system += MEMORY_NOTE.format(limit=args.notes_limit)
        tools.append(NOTES_TOOL)
    trace = Trace(args.out)
    trace.add("start", task_id=args.task_id, model=args.model, system=system, tools=[t["name"] for t in tools])
    chat = PROVIDERS[provider](model, system, tools, args.max_steps, args.effort, trace)

    state = json.loads(desk("inbox", "--json"))
    while not state["closed"]:
        trace.add("customer", turn=state["turn"], text=state["customer"], internal_note=state.get("internal_note"))
        shown = state["customer"]
        if state.get("internal_note"):  # e.g. a colleague's handoff note or a memo; the customer never sees it
            shown = f"[Internal note, not visible to the customer]\n{state['internal_note']}\n\n[Customer]\n{shown}"
        if NOTES["limit"] and (state["turn"] == 0 or str(state.get("internal_note") or "").startswith("Chat ")):
            chat.reset()  # a new chat: nothing is kept but the notes
            trace.add("memory", turn=state["turn"], notes=NOTES["text"])
            shown = f"[Your shift notes]\n{NOTES['text'] or '(nothing saved yet)'}\n\n{shown}"
        reply = chat.respond(shown) or "[The agent sent no message.]"
        trace.add("reply", turn=state["turn"], text=reply)
        state = json.loads(desk("reply", "--json", "-", stdin=reply))

    (args.out / "summary.json").write_text(json.dumps(
        {"task_id": args.task_id, "model": args.model, "agent": "fakeco-toolcall", "memory": args.memory, "hint": args.hint,
         **trace.usage}, indent=2))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:  # surface the reason in the task's error instead of a bare exit code
        error_path = os.environ.get("VALKYRIE_ERROR_PATH")
        if error_path:
            Path(error_path).write_text(f"{type(e).__name__}: {str(e)[:500]}\n")
        raise
