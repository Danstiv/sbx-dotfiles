"""The control socket: JSON over websockets, one frame per message.

Any number of clients may attach. Each is handed the current backlog on
connect, then every subsequent event as it happens; the first to answer a
prompt settles it and the rest are told by the broadcast. Nothing about a
pending call lives in this module — dropping a connection here has no effect on
what the gate is holding.
"""

import asyncio
import hmac
import http
import json
import logging
from typing import Any

import protocol
import websockets
from websockets.asyncio.server import ServerConnection, broadcast, serve

from .config import Config, DecisionStore, PolicyFile
from .gate import Gate

logger = logging.getLogger(__name__)


class ControlServer:
    def __init__(
        self,
        config: Config,
        store: DecisionStore,
        policy: PolicyFile,
        secret: str,
    ) -> None:
        self._config = config
        self._store = store
        self._secret = secret
        self._clients: set[ServerConnection] = set()
        self.gate = Gate(policy, store, self.publish)

    def publish(self, message: dict[str, Any]) -> None:
        """Fan a message out to every attached client.

        Synchronous and non-blocking by design: the gate calls this from the
        middle of holding a request, and a slow console must never be able to
        stall the proxy.
        """
        if self._clients:
            broadcast(self._clients, json.dumps(message, ensure_ascii=False))

    @staticmethod
    async def _send(ws: ServerConnection, message: dict[str, Any]) -> None:
        await ws.send(json.dumps(message, ensure_ascii=False))

    def _authorise(self, connection: ServerConnection, request) -> Any:
        header = request.headers.get("Authorization", "")
        scheme, _, token = header.partition(" ")
        # compare_digest, not ==, so a wrong token cannot be found byte by byte
        # from how long the rejection took.
        if scheme.lower() != "bearer" or not hmac.compare_digest(token, self._secret):
            logger.warning("rejected an unauthorised control connection")
            return connection.respond(http.HTTPStatus.UNAUTHORIZED, "bad or missing token\n")
        return None

    async def _handle(self, ws: ServerConnection) -> None:
        self._clients.add(ws)
        logger.info(f"client attached ({len(self._clients)} now)")
        try:
            await self._send(ws, protocol.hello(self.gate.snapshot()))
            async for raw in ws:
                await self._dispatch(ws, raw)
        except websockets.ConnectionClosed:
            pass
        finally:
            self._clients.discard(ws)
            logger.info(f"client detached ({len(self._clients)} left)")

    async def _dispatch(self, ws: ServerConnection, raw: str | bytes) -> None:
        try:
            message = json.loads(raw)
            if not isinstance(message, dict):
                raise ValueError("expected an object")
        except (ValueError, TypeError) as exc:
            await self._send(ws, protocol.error(f"unreadable message: {exc}"))
            return

        kind = message.get("type")
        if kind == protocol.Message.DECIDE:
            await self._decide(ws, message)
        elif kind == protocol.Message.LIST_DECISIONS:
            await self._send(ws, protocol.decisions_list(self._store.entries()))
        elif kind == protocol.Message.FORGET:
            removed = self._store.forget(
                message.get("sandbox", ""), message.get("host", ""), message.get("tool")
            )
            logger.info(f"forgot {removed} stored decision(s)")
            await self._send(ws, protocol.decisions_list(self._store.entries()))
        else:
            await self._send(ws, protocol.error(f"unknown message type: {kind!r}"))

    async def _decide(self, ws: ServerConnection, message: dict[str, Any]) -> None:
        request_id = message.get("id")
        try:
            decision = protocol.Decision(message.get("decision"))
        except ValueError:
            reported = message.get("decision")
            await self._send(ws, protocol.error(f"not a decision: {reported!r}", request_id))
            return
        if not isinstance(request_id, str) or not self.gate.decide(request_id, decision):
            # Already answered, timed out, or never existed.
            await self._send(ws, protocol.error("no longer pending", request_id))

    async def serve_forever(self) -> None:
        async with serve(
            self._handle,
            self._config.control_host,
            self._config.control_port,
            process_request=self._authorise,
        ):
            logger.info(
                f"control socket on ws://{self._config.control_host}"
                f":{self._config.control_port}"
            )
            await asyncio.get_running_loop().create_future()  # run until cancelled
