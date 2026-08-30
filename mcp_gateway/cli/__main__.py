"""Console client for the gate: `uv run -m cli`.

Attaches to the daemon's control socket, prints held tool calls, sends back
answers. Holds no state worth keeping — closing it does not release anything,
and reopening it replays whatever piled up meanwhile.
"""

import argparse
import asyncio
import json
import sys
import threading
from collections import deque
from pathlib import Path
from typing import Any

import protocol
import websockets
from daemon.config import Config, ConfigError, read_secret

RESET = "\033[0m"
DIM = "\033[2m"
BOLD = "\033[1m"
RED = "\033[31m"
YELLOW = "\033[33m"

# Why a prompt vanished, when it was not this console that answered it.
SETTLED_ELSEWHERE = {
    protocol.Source.CLIENT: "answered by another client",
    protocol.Source.TIMEOUT: "expired — denied",
    protocol.Source.CANCELLED: "the agent gave up — denied",
    protocol.Source.REMEMBERED: "covered by the answer you just gave",
}

# What each keystroke means. Anything else is not a decision — pressing Enter
# by accident must not be read as "deny".
ANSWERS = {
    "y": protocol.Decision.ONCE,
    "yes": protocol.Decision.ONCE,
    "a": protocol.Decision.ALWAYS,
    "always": protocol.Decision.ALWAYS,
    "n": protocol.Decision.DENY,
    "no": protocol.Decision.DENY,
}

# Said back once the daemon has the answer, so the keystroke visibly lands.
ANSWERED = {
    protocol.Decision.ONCE: "allowed, this once",
    protocol.Decision.ALWAYS: "allowed, and remembered",
    protocol.Decision.DENY: "denied",
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="mcp-gateway-cli", description=__doc__)
    parser.add_argument(
        "--config",
        type=Path,
        default=Path(__file__).resolve().parent.parent / "data" / "config.json",
        help="config.json, read for the control address and the secret",
    )
    parser.add_argument("--url", help="override the control socket URL")
    parser.add_argument("--secret", help="override the shared secret")
    parser.add_argument(
        "--full", action="store_true", help="print tool arguments untruncated"
    )
    return parser


def _read_stdin(loop: asyncio.AbstractEventLoop, queue: asyncio.Queue[str]) -> None:
    """One long-lived stdin reader.

    A per-prompt input() would strand a thread blocked on stdin every time a
    prompt is answered by somebody else or expires; a single reader makes those
    outcomes harmless.
    """
    for line in sys.stdin:
        loop.call_soon_threadsafe(queue.put_nowait, line.strip())


def render_arguments(arguments: Any, full: bool) -> str:
    text = json.dumps(arguments, ensure_ascii=False)
    if not full and len(text) > 400:
        text = text[:400] + " ..."
    return text


class Console:
    def __init__(self, full: bool) -> None:
        self.full = full
        self.queue: deque[dict[str, Any]] = deque()
        self._lines: asyncio.Queue[str] | None = None
        self._settled: dict[str, asyncio.Future[str]] = {}
        self._wake = asyncio.Event()

    def _stdin(self) -> asyncio.Queue[str]:
        if self._lines is None:
            self._lines = asyncio.Queue()
            threading.Thread(
                target=_read_stdin, args=(asyncio.get_running_loop(), self._lines), daemon=True
            ).start()
        return self._lines

    def offer(self, request: dict[str, Any]) -> None:
        self.queue.append(request)
        self._wake.set()

    def settle(self, request_id: str, decision: str, source: str) -> None:
        """Note that a request is no longer ours to answer."""
        self.queue = deque(item for item in self.queue if item["id"] != request_id)
        waiter = self._settled.get(request_id)
        if waiter is not None and not waiter.done():
            waiter.set_result(source)
        self._wake.set()

    async def run(self, send) -> None:
        while True:
            while not self.queue:
                self._wake.clear()
                await self._wake.wait()
            item = self.queue[0]
            answer = await self._ask(item)
            if answer is None:
                continue  # settled elsewhere; _ask already said so
            self.queue.popleft()
            await send(protocol.decide(item["id"], answer))
            # Only now, so the line means "the daemon has it" rather than
            # "a key was pressed".
            print(f"  {DIM}{ANSWERED[answer]}{RESET}")

    async def _ask(self, item: dict[str, Any]) -> protocol.Decision | None:
        choices = (
            "[y] once  [a] always  [n] deny"
            if item["offer_always"]
            else f"{YELLOW}[y] once  [n] deny{RESET}   (never remembered)"
        )
        print(f"\n  {DIM}sandbox{RESET} : {item['sandbox']}")
        print(f"  {DIM}server {RESET} : {item['host']}")
        print(f"  {BOLD}tool   {RESET} : {item['tool']}")
        print(f"  {DIM}args   {RESET} : {render_arguments(item['arguments'], self.full)}")
        print(f"  {choices} > ", end="", flush=True)

        # Race the operator against the daemon: whichever lands first ends the
        # prompt, so a request answered elsewhere clears the screen instead of
        # waiting for a keystroke that is no longer wanted.
        settled = asyncio.get_running_loop().create_future()
        self._settled[item["id"]] = settled
        try:
            while True:
                input_task = asyncio.create_task(self._stdin().get())
                done, _ = await asyncio.wait(
                    {input_task, settled}, return_when=asyncio.FIRST_COMPLETED
                )

                if settled in done:
                    input_task.cancel()
                    # A source this build does not know is a newer daemon, not
                    # another client — say what is true either way.
                    note = SETTLED_ELSEWHERE.get(settled.result(), "no longer waiting")
                    print(f"\r  {DIM}{note}{RESET}" + " " * 30)
                    return None

                answer = ANSWERS.get(input_task.result().strip().lower())
                if answer == protocol.Decision.ALWAYS and not item["offer_always"]:
                    answer = None  # this tool is asked about every time
                if answer is not None:
                    return answer
                # Not a decision. Say so and ask again rather than reading it
                # as "deny" — a stray Enter should not refuse the agent's call.
                print(f"  {DIM}not one of the choices{RESET}")
                print(f"  {choices} > ", end="", flush=True)
        finally:
            settled.cancel()
            self._settled.pop(item["id"], None)


async def session(url: str, secret: str, console: Console) -> None:
    async with websockets.connect(
        url, additional_headers={"Authorization": f"Bearer {secret}"}
    ) as ws:
        print(f"{DIM}attached to {url}{RESET}")

        async def send(message: dict[str, Any]) -> None:
            await ws.send(json.dumps(message, ensure_ascii=False))

        async def read() -> None:
            async for raw in ws:
                message = json.loads(raw)
                kind = message.get("type")
                if kind == protocol.Message.HELLO:
                    backlog = message.get("pending", [])
                    if backlog:
                        print(f"{DIM}{len(backlog)} call(s) already waiting{RESET}")
                    for item in backlog:
                        console.offer(item)
                elif kind == protocol.Message.REQUEST:
                    console.offer(message["request"])
                elif kind == protocol.Message.RESOLVED:
                    console.settle(message["id"], message["decision"], message["source"])
                elif kind == protocol.Message.EVENT:
                    if message["kind"] == protocol.Event.UNKNOWN_SANDBOX:
                        print(
                            f"\n  {RED}unknown sandbox{RESET}: {message.get('method')} on "
                            f"{message['host']} at {message.get('path')}\n"
                            f"  {DIM}add it to policy.json if this one is yours{RESET}"
                        )
                elif kind == protocol.Message.ERROR:
                    print(f"  {DIM}{message['message']}{RESET}")

        console_task = asyncio.create_task(console.run(send))
        reader_task = asyncio.create_task(read())
        try:
            # Whichever ends first ends the session. Waiting only on the reader
            # would leave a client whose prompt loop had died sitting there
            # looking attached and saying nothing, while calls piled up on the
            # daemon.
            done, _ = await asyncio.wait(
                {console_task, reader_task}, return_when=asyncio.FIRST_COMPLETED
            )
        finally:
            console_task.cancel()
            reader_task.cancel()

        # Re-raise whatever ended it. A closed socket is a WebSocketException
        # and reconnects upstairs; anything else is a bug in this client and
        # should be loud rather than swallowed by the cancel above.
        for task in done:
            await task


async def run(url: str, secret: str, full: bool) -> None:
    console = Console(full)
    delay = 1.0
    while True:
        try:
            await session(url, secret, console)
            delay = 1.0
            print(f"{DIM}daemon closed the connection{RESET}")
        except (OSError, websockets.WebSocketException) as exc:
            print(f"{DIM}not attached ({exc}); retrying in {delay:.0f}s{RESET}")
            await asyncio.sleep(delay)
            delay = min(delay * 2, 30.0)
        # Anything the daemon was holding stays held: reattaching replays it.
        console.queue.clear()


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    url, secret = args.url, args.secret
    if url is None or secret is None:
        try:
            config = Config.load(args.config)
        except ConfigError as exc:
            print(f"mcp-gateway-cli: {exc}", file=sys.stderr)
            return 2
        url = url or f"ws://{config.control_host}:{config.control_port}"
        secret = secret or read_secret(config.secret_path)
    try:
        asyncio.run(run(url, secret, args.full))
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
