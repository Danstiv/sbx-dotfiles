"""The holding pen: pending tool calls and the decisions that release them.

Deliberately knows nothing about mitmproxy or websockets. A pending request is
an id and a future, owned by this object — not by the connection that displayed
it and not by the socket that will answer it. That is what makes a client
dropping mid-prompt a non-event: the call is still held, and the next client to
connect is handed it in the snapshot.
"""

import asyncio
import itertools
import time
from dataclasses import dataclass, field
from typing import Any, Callable

import protocol

from .config import DecisionStore, PolicyFile


@dataclass(frozen=True)
class Verdict:
    """What the gate decided about one call, and what decided it.

    Not a Decision — "once" and "always" differ only in what gets written
    down. A refusal carries its cause, because that reaches the agent, and
    "the user said no" is not "the user was not there".
    """

    allowed: bool
    refused_by: protocol.Source | None = None


ALLOWED = Verdict(allowed=True)


@dataclass
class Pending:
    id: str
    sandbox: str
    host: str
    tool: str
    arguments: Any
    created_at: float
    expires_at: float
    offer_always: bool
    future: asyncio.Future[protocol.Decision] = field(repr=False)

    def payload(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "sandbox": self.sandbox,
            "host": self.host,
            "tool": self.tool,
            "arguments": self.arguments,
            "created_at": self.created_at,
            "expires_at": self.expires_at,
            "offer_always": self.offer_always,
        }


class Gate:
    def __init__(
        self,
        policy: PolicyFile,
        store: DecisionStore,
        publish: Callable[[dict[str, Any]], None],
    ) -> None:
        self._policy = policy
        self._store = store
        self._publish = publish
        self._pending: dict[str, Pending] = {}
        self._ids = itertools.count(1)

    def snapshot(self) -> list[dict[str, Any]]:
        """Everything currently held, for a client that just connected."""
        return [item.payload() for item in self._pending.values()]

    def decide(
        self,
        request_id: str,
        decision: protocol.Decision,
        source: protocol.Source = protocol.Source.CLIENT,
    ) -> bool:
        """Resolve a pending request. Returns False if it was already resolved.

        First answer wins: with several clients attached, whoever replies first
        settles it, and the rest are told so by the broadcast below rather than
        by an error.
        """
        item = self._pending.pop(request_id, None)
        if item is None:
            return False
        if decision == protocol.Decision.ALWAYS and not item.offer_always:
            decision = protocol.Decision.ONCE
        if decision == protocol.Decision.ALWAYS:
            self._store.remember(item.sandbox, item.host, item.tool)
        if not item.future.done():
            item.future.set_result(decision)
        self._store.audit(
            event="decision",
            request=item.id,
            sandbox=item.sandbox,
            host=item.host,
            tool=item.tool,
            decision=decision,
            source=source,
        )
        self._publish(protocol.resolved(item.id, decision, source))
        if decision == protocol.Decision.ALWAYS:
            self._release_covered_by(item)
        return True

    def _release_covered_by(self, decided: Pending) -> None:
        """Let an "always" cover the calls already waiting beside it.

        An agent fires a batch of tool calls at once, so all of them are held
        before the first one is answered — each checked the store back when
        nothing was stored yet. Without this the operator says "always" and is
        then asked the very same question about the rest of the batch, which
        reads as the answer not having worked.

        Matching the store's key exactly, tool included: this releases the calls
        the new rule already covers, and nothing else.
        """
        covered = [
            item
            for item in self._pending.values()
            if item.offer_always
            and (item.sandbox, item.host, item.tool)
            == (decided.sandbox, decided.host, decided.tool)
        ]
        for item in covered:
            del self._pending[item.id]
            if not item.future.done():
                # ONCE, not ALWAYS: the rule is already written down, and
                # storing it again from here would log a decision nobody made.
                item.future.set_result(protocol.Decision.ONCE)
            self._store.audit(
                event="decision",
                request=item.id,
                sandbox=item.sandbox,
                host=item.host,
                tool=item.tool,
                decision=protocol.Decision.ONCE,
                source=protocol.Source.REMEMBERED,
            )
            self._publish(
                protocol.resolved(
                    item.id, protocol.Decision.ONCE, protocol.Source.REMEMBERED
                )
            )

    async def ask(
        self,
        sandbox: str,
        host: str,
        tool: str,
        arguments: Any,
    ) -> Verdict:
        """Hold one tool call until it is answered, or until it times out.

        A stored "always" short-circuits without bothering anyone. Everything
        else waits: no client attached is not an error, it just means nobody has
        answered yet, and the timeout is what eventually ends it.
        """
        policy = self._policy.load()
        offer_always = policy.offers_always(tool)
        if offer_always and self._store.allowed(sandbox, host, tool):
            # No decision field: nobody decided anything here. The answer that
            # allowed this was given — and logged — when it was stored.
            self._store.audit(event="auto", sandbox=sandbox, host=host, tool=tool)
            self._publish(
                protocol.event(
                    protocol.Event.AUTO_ALLOWED, sandbox=sandbox, host=host, tool=tool
                )
            )
            return ALLOWED

        now = time.monotonic()
        wall = time.time()
        item = Pending(
            id=f"r{next(self._ids)}",
            sandbox=sandbox,
            host=host,
            tool=tool,
            arguments=arguments,
            created_at=wall,
            expires_at=wall + policy.timeout,
            offer_always=offer_always,
            future=asyncio.get_running_loop().create_future(),
        )
        self._pending[item.id] = item
        self._store.audit(
            event="held", request=item.id, sandbox=sandbox, host=host, tool=tool
        )
        self._publish(protocol.request(item.payload()))

        remaining = policy.timeout - (time.monotonic() - now)
        try:
            answer = await asyncio.wait_for(item.future, remaining)
            # Only a client can put a Decision in that future; timeout and
            # cancellation come out as the exceptions below.
            if answer == protocol.Decision.DENY:
                return Verdict(allowed=False, refused_by=protocol.Source.CLIENT)
            return ALLOWED
        except asyncio.TimeoutError:
            # wait_for cancelled the future; resolve the request ourselves so
            # every attached client learns why the prompt vanished.
            self._pending.pop(item.id, None)
            self._store.audit(
                event="decision",
                request=item.id,
                sandbox=sandbox,
                host=host,
                tool=tool,
                decision=protocol.Decision.DENY,
                source=protocol.Source.TIMEOUT,
            )
            self._publish(
                protocol.resolved(item.id, protocol.Decision.DENY, protocol.Source.TIMEOUT)
            )
            return Verdict(allowed=False, refused_by=protocol.Source.TIMEOUT)
        except asyncio.CancelledError:
            # The intercepted connection went away — the agent already gave up
            # waiting. Forwarding now would run the tool for real while the
            # agent believes it failed, so drop it and clear the prompt.
            self._pending.pop(item.id, None)
            self._store.audit(
                event="decision",
                request=item.id,
                sandbox=sandbox,
                host=host,
                tool=tool,
                decision=protocol.Decision.DENY,
                source=protocol.Source.CANCELLED,
            )
            self._publish(
                protocol.resolved(item.id, protocol.Decision.DENY, protocol.Source.CANCELLED)
            )
            raise
