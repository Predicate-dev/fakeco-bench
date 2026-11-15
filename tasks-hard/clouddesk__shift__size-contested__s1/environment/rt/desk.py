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

The conversation script and tool behaviour come from fakeco.core, the same code
the in-process harness runs, so a scenario plays out identically in both.
"""
from __future__ import annotations
import copy
import fcntl
import json
import sys
import time
from pathlib import Path
from types import SimpleNamespace

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from fakeco.core import execute, next_customer_message  # noqa: E402
from fakeco.worlds import WORLDS  # noqa: E402

DEFAULT_HOME = Path("/opt/fakeco")
CLOSED = "[chat closed] There is nothing more to send."
USAGE = __doc__.split("\n\n")[2]
# Scenario fields the conversation engine reads; older scenario files may lack the newer ones.
DEFAULTS = {"faults": {}, "confirm_after": None, "preface": "", "notes": {}, "checkpoints": {},
            "record_faults": {}, "turn_tickets": [], "tools": "core", "resume": None}


class Desk:
    def __init__(self, home: Path = DEFAULT_HOME):
        self.home = Path(home)
        self.state = self.home / "state"
        self.sc = SimpleNamespace(**{**DEFAULTS, **json.loads((self.home / "rt" / "scenario.json").read_text())})
        self.world = WORLDS[self.sc.world]

    # ------------------------------------------------------------ state
    def _read_lines(self, name: str) -> list[dict]:
        path = self.state / name
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]

    def _append(self, name: str, row: dict) -> None:
        with (self.state / name).open("a") as f:
            f.write(json.dumps(row) + "\n")

    def _chat(self) -> list[dict]:
        return self._read_lines("transcript.jsonl")

    def _log(self) -> list[dict]:
        return self._read_lines("log.jsonl")

    def _turn(self) -> int:
        """Index of the message currently waiting for a reply."""
        return sum(1 for m in self._chat() if m["role"] == "assistant")

    def _deliver(self) -> dict | None:
        """Append whatever the customer says next to the transcript; None once the chat is closed."""
        message = next_customer_message(self.sc, self.world, self._chat(), self._log())
        if message is not None:
            self._append("transcript.jsonl", {"role": "user", "turn": self._turn(), **message, "ts": time.time()})
        return message

    def _init(self) -> None:
        self.state.mkdir(parents=True, exist_ok=True)
        if not (self.state / "db.json").exists():
            (self.state / "db.json").write_text(json.dumps(copy.deepcopy(self.sc.db)))
            self._deliver()

    def _waiting(self) -> dict | None:
        chat = self._chat()
        return chat[-1] if chat and chat[-1]["role"] == "user" else None

    # ------------------------------------------------------------ commands
    def status(self, **extra) -> str:
        """Machine-readable chat state: the message awaiting a reply, or closed."""
        waiting = self._waiting()
        return json.dumps({**extra, "closed": waiting is None, "turn": self._turn(),
                           "customer": waiting["text"] if waiting else None,
                           "internal_note": waiting.get("note") if waiting else None})

    @staticmethod
    def _show(message: dict) -> str:
        note = f"[internal note, not visible to the customer] {message['note']}\n" if message.get("note") else ""
        return f"{note}[customer] {message['text']}"

    def inbox(self) -> str:
        lines = [self._show(m) if m["role"] == "user" else f"[you] {m['text']}" for m in self._chat()]
        lines.append(CLOSED if self._waiting() is None
                     else "\n(Someone is waiting. Answer with: desk reply 'your message')")
        return "\n".join(lines)

    def tools(self) -> str:
        return json.dumps(self.world.tool_specs(extended=self.sc.tools == "extended"), indent=2)

    def call(self, name: str, args) -> str:
        known = [t["name"] for t in self.world.tool_specs(extended=self.sc.tools == "extended")]
        if name not in known:
            return json.dumps({"error": f"Unknown tool: {name}. Available tools: {known}"})
        if not isinstance(args, dict):
            return json.dumps({"error": "Tool arguments must be a JSON object."})
        db, log = json.loads((self.state / "db.json").read_text()), self._log()
        result = execute(self.sc, self.world, db, log, self._turn(), name, args)
        (self.state / "db.json").write_text(json.dumps(db))
        self._append("log.jsonl", {**log[-1], "ts": time.time()})
        return json.dumps(result, indent=2)

    def reply(self, text: str) -> str:
        text = text.strip()
        if not text:
            return "error: empty message; nothing was sent."
        if self._waiting() is None:
            return "error: the chat is already closed; your message was not delivered."
        self._append("transcript.jsonl", {"role": "assistant", "turn": self._turn(), "text": text, "ts": time.time()})
        message = self._deliver()
        return "[delivered]\n\n" + (CLOSED if message is None else self._show(message))


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
