"""Running the gate in the background: `-d`, `--stop`, `--status`.

Windows cannot fork, so this is not the POSIX double-fork that sheds the
current process. It starts a *second* one — no console window, output appended
to `daemon.log` — and the first waits only long enough to watch the control
port come up.

Liveness is the control port, never the pidfile. "Is the gate up" is a question
about whether it answers, and a pid outlives the process that owned it. The
pidfile only says whom to stop.
"""

import os
import signal
import socket
import subprocess
import sys
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

from .config import Config

# Generous: a cold Python start on Windows plus mitmproxy issuing its CA on the
# very first run. Overshooting costs nothing — the wait ends as soon as the
# port answers, and the only thing the timeout decides is when to give up.
STARTUP_TIMEOUT = 30.0
STOP_TIMEOUT = 10.0

# Where `-m daemon` resolves from, so the child does not depend on the cwd the
# parent happened to be started in.
ROOT = Path(__file__).resolve().parent.parent


@contextmanager
def claim_pidfile(config: Config) -> Iterator[None]:
    config.pid_path.write_text(f"{os.getpid()}\n", "utf-8")
    try:
        yield
    finally:
        if _read_pid(config) == os.getpid():
            config.pid_path.unlink(missing_ok=True)


def _address(config: Config) -> str:
    return f"{config.control_host}:{config.control_port}"


def _answering(config: Config, timeout: float = 0.3) -> bool:
    """Is anything accepting connections on the control port?"""
    try:
        with socket.create_connection(
            (config.control_host, config.control_port), timeout
        ):
            return True
    except OSError:
        return False


def _wait(until: Callable[[], bool], timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if until():
            return True
        time.sleep(0.1)
    return until()


def _read_pid(config: Config) -> int | None:
    try:
        return int(config.pid_path.read_text("utf-8").strip())
    except (OSError, ValueError):
        return None


def _tail(path: Path, lines: int = 12) -> str:
    try:
        text = path.read_text("utf-8", errors="replace")
    except OSError:
        return ""
    return "".join(f"  {line}" for line in text.splitlines(keepends=True)[-lines:])


def _detached() -> dict[str, object]:
    """Popen flags for a child that outlives this process."""
    if sys.platform == "win32":
        return {"creationflags": subprocess.CREATE_NO_WINDOW}
    return {"start_new_session": True}


def _disown(child: subprocess.Popen) -> None:
    """Stop Popen from treating a deliberately orphaned child as a leak."""
    child.returncode = 0


def start(config: Config, argv: list[str]) -> int:
    """Start the daemon in the background and wait for it to answer."""
    if _answering(config):
        print(f"mcp-gateway: already running on {_address(config)}")
        return 0

    command = [sys.executable, "-m", "daemon", *argv]
    with config.log_path.open("a", encoding="utf-8") as log:
        log.write(f"=== {time.strftime('%Y-%m-%d %H:%M:%S')} starting ===\n")
        log.flush()
        child = subprocess.Popen(
            command,
            cwd=ROOT,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            close_fds=True,
            **_detached(),
        )

    # The pidfile is the child's to write; it does so before it binds anything,
    # so it is there by the time the port answers.
    deadline = time.monotonic() + STARTUP_TIMEOUT
    while time.monotonic() < deadline:
        if _answering(config):
            _disown(child)
            print(f"mcp-gateway: running on {_address(config)} (pid {child.pid})")
            print(f"mcp-gateway: logging to {config.log_path}")
            return 0
        if child.poll() is not None:
            # Died on the way up — a bad config, a port already taken. The log
            # has the reason and nobody would look there unbidden.
            print(
                f"mcp-gateway: exited at once (status {child.returncode}); "
                f"the last of {config.log_path}:",
                file=sys.stderr,
            )
            print(_tail(config.log_path), end="", file=sys.stderr)
            return 1
        time.sleep(0.1)

    # Alive but silent. Left running rather than killed: it may yet come up,
    # and --stop is one command away either way.
    _disown(child)
    print(
        f"mcp-gateway: pid {child.pid} started but {_address(config)} did not "
        f"answer within {STARTUP_TIMEOUT:.0f}s — see {config.log_path}",
        file=sys.stderr,
    )
    return 1


def stop(config: Config) -> int:
    pid = _read_pid(config)
    if not _answering(config):
        config.pid_path.unlink(missing_ok=True)
        print("mcp-gateway: not running")
        return 0
    if pid is None:
        print(
            f"mcp-gateway: something answers on {_address(config)} but "
            f"{config.pid_path} names nobody — stop it by hand",
            file=sys.stderr,
        )
        return 1

    # SIGTERM ends it abruptly on both platforms (on Windows os.kill is
    # TerminateProcess). Nothing here needs an orderly close: decisions.json is
    # replaced atomically, audit.jsonl is appended a line at a time, and a call
    # still being held would have been denied on timeout anyway.
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError as exc:
        print(f"mcp-gateway: cannot signal pid {pid}: {exc}", file=sys.stderr)
        return 1
    if not _wait(lambda: not _answering(config), STOP_TIMEOUT):
        print(
            f"mcp-gateway: signalled pid {pid} but {_address(config)} is still "
            "answering",
            file=sys.stderr,
        )
        return 1
    config.pid_path.unlink(missing_ok=True)
    print(f"mcp-gateway: stopped (pid {pid})")
    return 0


def status(config: Config) -> int:
    pid = _read_pid(config)
    if _answering(config):
        whose = f" (pid {pid})" if pid is not None else ""
        print(f"mcp-gateway: running on {_address(config)}{whose}")
        return 0
    print(f"mcp-gateway: not running (nothing on {_address(config)})")
    if pid is not None:
        print(f"mcp-gateway: {config.pid_path.name} still names pid {pid}; the last of the log:")
        print(_tail(config.log_path), end="")
    return 1
