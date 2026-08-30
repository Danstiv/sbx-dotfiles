"""Wire format for the control socket, shared by the daemon and every client.
"""

from enum import StrEnum
from typing import Any

VERSION = 1

# StrEnum throughout: these are wire values, so they have to serialise as the
# plain strings they are, while still being a closed set the type checker can
# see and a client can enumerate.


class Decision(StrEnum):
    """What an operator may answer."""

    ONCE = "once"
    ALWAYS = "always"
    DENY = "deny"


class Source(StrEnum):
    """How a pending request came to be resolved.

    Reported back to every client so a second client can tell "someone else
    answered" from "it timed out".
    """

    CLIENT = "client"
    TIMEOUT = "timeout"
    CANCELLED = "cancelled"
    REMEMBERED = "remembered"


class Message(StrEnum):
    """The `type` of a frame, in both directions."""

    # Client -> daemon
    DECIDE = "decide"
    LIST_DECISIONS = "list_decisions"
    FORGET = "forget"

    # Daemon -> client
    HELLO = "hello"
    REQUEST = "request"
    RESOLVED = "resolved"
    DECISIONS = "decisions"
    EVENT = "event"
    ERROR = "error"


class Event(StrEnum):
    """Traffic the gate saw but did not hold.

    Purely observational, but without these an unlisted sandbox looks like
    "MCP is broken" rather than like a line missing from the config.
    """

    PASSTHROUGH = "passthrough"
    UNKNOWN_SANDBOX = "unknown_sandbox"
    AUTO_ALLOWED = "auto_allowed"


# ----------------------------------------------------------- daemon -> client

def hello(pending: list[dict[str, Any]]) -> dict[str, Any]:
    """First frame on every connection.

    Carries the full backlog: a client that starts late, or reconnects after a
    drop, still sees everything currently held.
    """
    return {"type": Message.HELLO, "protocol": VERSION, "pending": pending}


def request(payload: dict[str, Any]) -> dict[str, Any]:
    return {"type": Message.REQUEST, "request": payload}


def resolved(request_id: str, decision: Decision, source: Source) -> dict[str, Any]:
    return {
        "type": Message.RESOLVED,
        "id": request_id,
        "decision": decision,
        "source": source,
    }


def decisions_list(entries: list[dict[str, Any]]) -> dict[str, Any]:
    return {"type": Message.DECISIONS, "entries": entries}


def event(kind: Event, **fields: Any) -> dict[str, Any]:
    return {"type": Message.EVENT, "kind": kind, **fields}


def error(message: str, ref: str | None = None) -> dict[str, Any]:
    return {"type": Message.ERROR, "message": message, "ref": ref}


# ----------------------------------------------------------- client -> daemon

def decide(request_id: str, decision: Decision) -> dict[str, Any]:
    return {"type": Message.DECIDE, "id": request_id, "decision": decision}


def list_decisions() -> dict[str, Any]:
    return {"type": Message.LIST_DECISIONS}


def forget(sandbox: str, host: str, tool: str | None = None) -> dict[str, Any]:
    """Drop a stored decision. Omit `tool` to drop every tool for that pair."""
    return {"type": Message.FORGET, "sandbox": sandbox, "host": host, "tool": tool}
