"""The support desk an agent works through inside a FakeCo task sandbox.

Installed as /opt/fakeco/rt/desk, owned by the `fakeco` account and reached only
through `sudo -u fakeco`, so the agent can use the desk but cannot read the
scenario (future customer turns, injected faults) or edit the ledger and logs.

    desk inbox                     show the chat so far
    desk tools                     list back-office tools and their argument schemas
    desk call TOOL '{"k": "v"}'    run a back-office tool (JSON args, or k=v pairs, or - for stdin)
    desk reply 'message'           send a message to the customer, print their next one
    (inbox and reply take --json for harnesses that drive the desk from code)

State lives in <home>/state: db.json (world state), log.jsonl (tool calls),
transcript.jsonl (chat). The verifier scores from those files.
"""
from __future__ import annotations
import copy
import fcntl
import json
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from fakeco.core import CONFIRMATIONS, FAULTS  # noqa: E402
from fakeco.worlds import WORLDS, ToolError  # noqa: E402

DEFAULT_HOME = Path("/opt/fakeco")
CLOSED = "[chat closed] The customer has left the chat. There is nothing more to send."
USAGE = __doc__.split("\n\n")[2]


class Desk:
    def __init__(self, home: Path = DEFAULT_HOME):
        self.home = Path(home)
        self.state = self.home / "state"
        self.scenario = json.loads((self.home / "rt" / "scenario.json").read_text())
        self.world = WORLDS[self.scenario["world"]]
        self.turns = self.scenario["user_turns"]

    # ------------------------------------------------------------ state
    def _read_lines(self, name: str) -> list[dict]:
        path = self.state / name
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]

    def _append(self, name: str, row: dict) -> None:
        with (self.state / name).open("a") as f:
            f.write(json.dumps(row) + "\n")

    def _init(self) -> None:
        self.state.mkdir(parents=True, exist_ok=True)
        if not (self.state / "db.json").exists():
            (self.state / "db.json").write_text(json.dumps(copy.deepcopy(self.scenario["db"])))
            self._append("transcript.jsonl", {"role": "user", "turn": 0, "text": self.turns[0], "ts": time.time()})

    def _chat(self) -> list[dict]:
        return self._read_lines("transcript.jsonl")

    def _turn(self) -> int:
        """Index of the customer message currently waiting for a reply."""
        return sum(1 for m in self._chat() if m["role"] == "assistant")

    def _waiting(self) -> str | None:
        """The customer message awaiting a reply, or None once the chat is closed."""
        chat = self._chat()
        return chat[-1]["text"] if chat and chat[-1]["role"] == "user" else None

    def _next_customer_message(self) -> tuple[str, bool] | None:
        """What the customer says after the reply just sent, as (text, scripted); None closes the chat.

        The script is fixed, with one exception: at the point where it assumes the request is being
        carried out, a customer who sees that nothing has been attempted yet confirms ("Yes, please go
        ahead.") before the script moves on. Without that, an agent that asks before an irreversible
        action would be answered by a customer who talks past the question.
        """
        chat = self._chat()
        scripted = sum(1 for m in chat if m["role"] == "user" and m.get("scripted", True))
        confirmed = sum(1 for m in chat if m["role"] == "user" and not m.get("scripted", True))
        attempted = any(e["tool"] == self.world.money_tool for e in self._read_lines("log.jsonl"))
        if scripted == self.scenario.get("confirm_after") and confirmed < len(CONFIRMATIONS) and not attempted:
            return CONFIRMATIONS[confirmed], False
        if scripted < len(self.turns):
            return self.turns[scripted], True
        return None

    # ------------------------------------------------------------ commands
    def status(self, **extra) -> str:
        """Machine-readable chat state: the message awaiting a reply, or closed."""
        waiting, turn = self._waiting(), self._turn()
        note = self.scenario.get("preface") or None
        return json.dumps({**extra, "closed": waiting is None, "turn": turn, "customer": waiting,
                           "internal_note": note if turn == 0 else None})

    def inbox(self) -> str:
        lines = []
        if self.scenario.get("preface"):
            lines.append(f"[internal note, not visible to the customer] {self.scenario['preface']}\n")
        for m in self._chat():
            who = "customer" if m["role"] == "user" else "you"
            lines.append(f"[{who}] {m['text']}")
        if self._waiting() is None:
            lines.append(CLOSED)
        else:
            lines.append("\n(The customer is waiting. Answer with: desk reply 'your message')")
        return "\n".join(lines)

    def tools(self) -> str:
        return json.dumps(self.world.tool_specs(), indent=2)

    def call(self, name: str, args) -> str:
        known = [t["name"] for t in self.world.tool_specs()]
        if name not in known:
            return json.dumps({"error": f"Unknown tool: {name}. Available tools: {known}"})
        if not isinstance(args, dict):
            return json.dumps({"error": "Tool arguments must be a JSON object."})
        fault = self.scenario.get("faults", {}).get(name)
        if fault:
            result = FAULTS[fault](args)
        else:
            db = json.loads((self.state / "db.json").read_text())
            try:
                result = self.world.dispatch(db, name, args)
                (self.state / "db.json").write_text(json.dumps(db))
            except ToolError as e:
                result = {"error": str(e)}
            except Exception as e:  # malformed arguments must not crash the desk
                result = {"error": f"Invalid arguments: {type(e).__name__}"}
        self._append("log.jsonl", {"turn": self._turn(), "tool": name, "args": args, "result": result,
                                   "faulted": bool(fault), "ts": time.time()})
        return json.dumps(result, indent=2)

    def reply(self, text: str) -> str:
        text = text.strip()
        if not text:
            return "error: empty message; nothing was sent."
        if self._waiting() is None:
            return "error: the chat is already closed; your message was not delivered."
        i = self._turn()
        self._append("transcript.jsonl", {"role": "assistant", "turn": i, "text": text, "ts": time.time()})
        nxt = self._next_customer_message()
        if nxt is None:
            return "[delivered]\n\n" + CLOSED
        message, scripted = nxt
        self._append("transcript.jsonl", {"role": "user", "turn": i + 1, "text": message, "scripted": scripted,
                                          "ts": time.time()})
        return f"[delivered]\n\n[customer] {message}"


def _parse_args(rest: list[str], stdin) -> dict | str:
    if not rest:
        return {}
    raw = stdin.read() if rest == ["-"] else None
    if raw is None and len(rest) == 1 and rest[0].lstrip().startswith("{"):
        raw = rest[0]
    if raw is not None:
        try:
            return json.loads(raw)
        except json.JSONDecodeError as e:
            return f"error: arguments are not valid JSON ({e.msg})."
    out = {}
    for pair in rest:  # k=v pairs
        if "=" not in pair:
            return "error: pass arguments as one JSON object or as key=value pairs."
        k, v = pair.split("=", 1)
        try:
            out[k] = json.loads(v)
        except json.JSONDecodeError:
            out[k] = v
    return out


def main(argv: list[str], home: Path = DEFAULT_HOME, stdin=None, out=None) -> int:
    stdin, out = stdin or sys.stdin, out or sys.stdout
    if not argv or argv[0] in ("help", "-h", "--help"):
        print(USAGE, file=out)
        return 0
    desk = Desk(home)
    desk.state.mkdir(parents=True, exist_ok=True)
    with (desk.state / ".lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        desk._init()
        cmd, rest = argv[0], argv[1:]
        as_json = "--json" in rest and cmd in ("inbox", "reply")
        if as_json:
            rest = [a for a in rest if a != "--json"]
        if cmd == "inbox":
            print(desk.status() if as_json else desk.inbox(), file=out)
        elif cmd == "tools":
            print(desk.tools(), file=out)
        elif cmd == "call":
            if not rest:
                print("error: usage: desk call TOOL '{\"arg\": \"value\"}'", file=out)
                return 2
            args = _parse_args(rest[1:], stdin)
            if isinstance(args, str):
                print(args, file=out)
                return 2
            print(desk.call(rest[0], args), file=out)
        elif cmd == "reply":
            text = stdin.read() if (not rest or rest == ["-"]) else " ".join(rest)
            msg = desk.reply(text)
            failed = msg.startswith("error:")
            if as_json:
                print(desk.status(delivered=not failed, **({"error": msg} if failed else {})), file=out)
            else:
                print(msg, file=out)
            return 2 if failed else 0
        else:
            print(f"error: unknown command {cmd!r}.\n\n{USAGE}", file=out)
            return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
