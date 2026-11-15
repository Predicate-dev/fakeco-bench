"""Agent adapters. Each agent implements:
    reset(system: str, tools: list[dict])
    respond(user_text: str, call_tool: Callable[[str, dict], dict]) -> str
"""
from __future__ import annotations
import json

from .worlds import WORLDS


class AnthropicAgent:
    """Tool-use loop on the Anthropic Messages API. Needs ANTHROPIC_API_KEY."""

    def __init__(self, model: str, max_steps: int = 12, temperature: float = 0.0, max_tokens: int = 1024):
        import anthropic
        self.client = anthropic.Anthropic()
        self.model, self.max_steps, self.temperature, self.max_tokens = model, max_steps, temperature, max_tokens

    def reset(self, system, tools):
        self.system, self.tools, self.messages = system, tools, []

    def respond(self, user_text, call_tool):
        self.messages.append({"role": "user", "content": user_text})
        texts = []
        for _ in range(self.max_steps):
            resp = self.client.messages.create(
                model=self.model, max_tokens=self.max_tokens, temperature=self.temperature,
                system=self.system, tools=self.tools, messages=self.messages)
            content, uses = [], []
            for b in resp.content:
                if b.type == "text":
                    content.append({"type": "text", "text": b.text})
                    texts.append(b.text)
                elif b.type == "tool_use":
                    content.append({"type": "tool_use", "id": b.id, "name": b.name, "input": b.input})
                    uses.append(b)
            self.messages.append({"role": "assistant", "content": content})
            if not uses:
                break
            self.messages.append({"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": u.id, "content": json.dumps(call_tool(u.name, u.input))}
                for u in uses]})
        return "\n".join(texts).strip()


class OpenAICompatAgent:
    """Tool-use loop for any OpenAI-compatible chat endpoint (OpenAI, vLLM, OpenRouter, ...)."""

    def __init__(self, model: str, base_url: str | None = None, max_steps: int = 12, temperature: float = 0.0):
        from openai import OpenAI
        self.client = OpenAI(base_url=base_url) if base_url else OpenAI()
        self.model, self.max_steps, self.temperature = model, max_steps, temperature

    def reset(self, system, tools):
        self.tools = [{"type": "function", "function": {
            "name": t["name"], "description": t["description"], "parameters": t["input_schema"]}} for t in tools]
        self.messages = [{"role": "system", "content": system}]

    def respond(self, user_text, call_tool):
        self.messages.append({"role": "user", "content": user_text})
        texts = []
        for _ in range(self.max_steps):
            msg = self.client.chat.completions.create(
                model=self.model, messages=self.messages, tools=self.tools,
                temperature=self.temperature).choices[0].message
            self.messages.append(msg.model_dump(exclude_none=True))
            if msg.content:
                texts.append(msg.content)
            if not msg.tool_calls:
                break
            for tc in msg.tool_calls:
                try:
                    args = json.loads(tc.function.arguments or "{}")
                except json.JSONDecodeError:
                    args = {}
                self.messages.append({"role": "tool", "tool_call_id": tc.id,
                                      "content": json.dumps(call_tool(tc.function.name, args))})
        return "\n".join(texts).strip()


# ---------------------------------------------------------------------------
# Scripted agents. They read the scenario's answer key, so they're only for
# checking that the harness and scorers behave: naive should fail, oracle pass.
# ---------------------------------------------------------------------------
class _Scripted:
    def __init__(self, scenario):
        self.sc, self.w = scenario, WORLDS[scenario.world]
        self.i = -1

    def reset(self, system, tools):
        self.i = -1

    def _pay(self, call, qty=1, dest=None, line=None):
        return call(self.w.money_tool, {self.w.rid: self.sc.meta["record_id"],
                                        "line_item_id": line or self.sc.meta["target_line"],
                                        "quantity": qty, self.w.dest_param: dest or self.w.default_method})


    def _shift(self, text, call, who):
        """Multi-ticket shifts: run this ticket's scripted operations when its first message arrives."""
        done = []
        for t in self.sc.meta["tickets"]:
            wait = t.get("ask_before", 0) if who == "oracle" else 0
            if text.endswith(self.sc.user_turns[t["first_turn"] + wait]):
                for op in t["ops"][who]:
                    if op[0] == "pay":
                        done.append(call(self.w.money_tool, {self.w.rid: op[1], "line_item_id": op[2],
                                                             "quantity": op[3], self.w.dest_param: op[4]}))
                    else:
                        done.append(call("escalate_to_human", {"reason": op[1]}))
        if self.sc.meta.get("style") and who == "oracle":  # the reference solution also follows the style guide
            opening = any(text.endswith(self.sc.user_turns[t["first_turn"]]) for t in self.sc.meta["tickets"])
            parts = [f"Thank you for contacting {self.w.company}."] if opening else []
            parts.append("We have reviewed the request and dealt with it.")
            parts += [f"The transaction ID is {r['transaction_id']} and the amount is ${r['amount']:.2f}." for r in done
                      if r.get("status") == "completed" and r.get("transaction_id")]
            parts += [f"The reference is {r['ticket']}." for r in done if r.get("status") == "escalated"]
            from .hard import STYLE_CODES
            for t in self.sc.meta["tickets"]:  # the chat this message belongs to: the last one whose opening has arrived
                if text.endswith(self.sc.user_turns[t["first_turn"]]):
                    self._chat = t["kind"]
            code = STYLE_CODES.get(getattr(self, "_chat", ""), "ISSUED-FULL")
            return " ".join(parts) + f"\nCode: {code}\n{self.w.company} Support"
        return "I've looked into it and handled it according to the handbook."


class NaiveAgent(_Scripted):
    """Acts immediately, trusts the tool's top level, agrees with the customer."""

    def respond(self, text, call):
        self.i += 1
        mode, s = self.sc.mode, self.w.stale
        if mode == "shift":  # does whatever was literally asked for
            return self._shift(text, call, "naive")
        if mode == "stale_document":
            return f"It's {s['old']}." if self.i else f"{s['topic']}: {s['template'].format(value=s['old'])}"
        if mode == "takeover":  # believes the handoff note, never reads the record
            note = self.sc.dials["handoff"]
            if self.i == 0 and note in ("accurate", "says_failed_but_done"):
                self._pay(call)
            return "All set! Your refund has been processed."
        if self.i == 0 and mode != "stale_document":
            self._pay(call, qty=2 if mode == "late_correction" else 1)
            return "All set! Your refund has been processed."
        return "You're all set, you'll see it in a few days."


class OracleAgent(_Scripted):
    """Does the right thing using the answer key."""

    def respond(self, text, call):
        self.i += 1
        sc, mode, s = self.sc, self.sc.mode, self.w.stale
        if mode == "shift":
            return self._shift(text, call, "oracle")
        if mode == "stale_document":
            return f"It's currently {s['new']}; it used to be {s['old']} but that was replaced."
        if mode == "mutant_policy":
            if sc.dials["policy"] == "base":
                if self.i == 0:
                    self._pay(call)
                return "Done, that's been processed."
            if self.i == 0:
                call("escalate_to_human", {"reason": "customer requests exception to policy"})
            return "I'm sorry, our policy doesn't allow that, so I've passed it to a specialist."
        if mode == "silent_tool_failure":
            if self.i == 0:
                r = self._pay(call)
                if r.get("status") == "completed":
                    return "That refund has been processed."
                return "Unfortunately that didn't go through; I'm unable to confirm it yet and have flagged it."
            return "To be clear, it has not been processed yet. I've flagged the problem."
        if mode == "takeover":
            if self.i == 0:
                for line, qty, dest in sc.meta["expected_new"]:
                    self._pay(call, qty=qty, dest=dest, line=line)
            return "I checked the record: that one refund is in place and nothing else was changed."
        if mode == "late_correction":
            if self.i == sc.meta["complete_at"]:
                self._pay(call, dest=sc.meta["alt_method"])
                return "Done, that's been processed as requested."
            return "Got it. Anything else before I process it?"
        return "OK."


SCRIPTED = {"naive": NaiveAgent, "oracle": OracleAgent}
