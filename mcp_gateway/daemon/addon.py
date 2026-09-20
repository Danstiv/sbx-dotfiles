"""The mitmproxy addon: the only part that touches intercepted traffic.

Requests are buffered and parsed, because a gate has to read what it approves.
Responses are never buffered — MCP's Streamable HTTP transport answers with
`text/event-stream` and may hold a stream open indefinitely, so anything that
waits for a response body to end is a hang waiting to happen.
"""

import json
import logging
from dataclasses import dataclass
from typing import Any

import protocol
from mitmproxy import http

from .config import MAX_BODY, PolicyFile
from .gate import Gate

logger = logging.getLogger(__name__)

# JSON-RPC 2.0 error codes. The standard block is re-exported by the MCP schema
# (schema/*/schema.ts) as PARSE_ERROR ... INTERNAL_ERROR; -32600 is the one this
# addon has cause to send.
INVALID_REQUEST = -32600

# -32000 to -32099 is the range JSON-RPC reserves for implementation-defined
# server errors, and MCP adds nothing of its own there. This is the gate saying
# it will not vouch for a request, which is a statement about the gate rather
# than about the request being malformed.
REFUSED_BY_GATE = -32000

# What the agent is told when a call does not go through. It acts on this, so
# it has to say which of these actually happened: a refusal and an unattended
# console call for very different things next.
REFUSAL = {
    protocol.Source.CLIENT: "Denied by the user: {tool}",
    protocol.Source.TIMEOUT: (
        "The user did not answer within the timeout, so {tool} was not run. "
        "This is not a refusal — ask again when the user is back at the gate."
    ),
}


SANDBOX_HEADER = "X-Sbx-Sandbox"


def take_sandbox(request: http.Request, sandboxes: tuple[str, ...]) -> str | None:
    """Read the sandbox name out of the request, taking the header with it.

    The header is ours, not the server's, so it is removed either way. A name
    the policy does not list is no name at all.
    """
    name = request.headers.pop(SANDBOX_HEADER, None)
    return name if name in sandboxes else None


@dataclass(frozen=True)
class ToolCall:
    id: Any
    tool: str
    arguments: Any


def tool_call(body: dict[str, Any]) -> ToolCall | None:
    """The tools/call in a request body, if that is what it is.

    One at most: JSON-RPC batching was removed from MCP in the 2025-06-18
    revision, so a body carries exactly one request.
    """
    if body.get("method") != "tools/call":
        return None
    params = body.get("params") if isinstance(body.get("params"), dict) else {}
    return ToolCall(body.get("id"), params.get("name") or "<unnamed>", params.get("arguments"))


def _respond(payload: dict[str, Any]) -> http.Response:
    """Answer the request ourselves, without it ever leaving the machine.

    Always 200: sbx reads a 4xx from the backend as "not authorized" and parks
    the whole server until it is kicked by hand.
    """
    return http.Response.make(
        200,
        json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        {"Content-Type": "application/json"},
    )


def refusal(call: ToolCall, reason: str) -> http.Response:
    """A refused call is a *successful* response carrying isError.

    The agent should see one failed tool, not a dead connection.
    """
    return _respond(
        {
            "jsonrpc": "2.0",
            "id": call.id,
            "result": {"content": [{"type": "text", "text": reason}], "isError": True},
        }
    )


def protocol_error(code: int, reason: str) -> http.Response:
    """For a body not parsed far enough to name a tool.

    An error rather than a tool result: with no request id there is no call to
    attribute one to, and a null id is what JSON-RPC prescribes for that.
    """
    return _respond({"jsonrpc": "2.0", "id": None, "error": {"code": code, "message": reason}})


class GateAddon:
    def __init__(
        self,
        policy: PolicyFile,
        gate: Gate,
        publish,
        tokens: dict[str, str] | None = None,
    ) -> None:
        self._policy = policy
        self._gate = gate
        self._publish = publish
        self._tokens = tokens or {}

    def _gated(self, flow: http.HTTPFlow) -> bool:
        return flow.request.pretty_host in self._policy.load().hosts

    async def request(self, flow: http.HTTPFlow) -> None:
        if not self._gated(flow):
            return

        host = flow.request.pretty_host
        if logger.isEnabledFor(logging.DEBUG):
            # The only view of what sbx actually put on the wire, and the one
            # place the marker is still visible — it is taken off just below.
            shown = {
                name: "<redacted>" if name.lower() == "authorization" else value
                for name, value in flow.request.headers.items()
            }
            logger.debug(f"{flow.request.method} {host}{flow.request.path} {shown}")

        sandbox = take_sandbox(flow.request, self._policy.load().sandboxes)
        if sandbox is not None:
            token = self._tokens.get(host)
            if token is not None:
                # Lent only to a request that named a sandbox we know. Without
                # that the gate cannot say whose call this is, and an anonymous
                # caller is not one to hand a credential to.
                flow.request.headers["Authorization"] = f"Bearer {token}"

        if len(flow.request.raw_content or b"") > MAX_BODY:
            # Not INVALID_REQUEST: nothing was parsed, so there is no ground to
            # call the request malformed. It is the gate declining to inspect.
            flow.response = protocol_error(
                REFUSED_BY_GATE, "Refused: request body too large to inspect."
            )
            return

        try:
            body = json.loads(flow.request.get_text() or "")
        except Exception:
            return  # OAuth form posts and anything else that is not JSON

        if not isinstance(body, dict):
            # A JSON array would be a JSON-RPC batch, dropped from MCP in the
            # 2025-06-18 revision. Refusing beats passing it on: a batch of
            # tools/call is the one shape that could slip through ungated.
            logger.warning(f"refusing a non-object JSON body on {host}")
            flow.response = protocol_error(
                INVALID_REQUEST, "Refused: JSON-RPC batching is not supported."
            )
            return

        method = body.get("method")
        call = tool_call(body)

        if sandbox is None and isinstance(method, str):
            # Announced for any MCP method, not only tools/call. The first
            # thing a session sends is initialize, and that is the moment a
            # server registered without the marker shows up — waiting for a
            # tool call means waiting for one that may never come.
            #
            # Only the tool call is refused; the rest is forwarded, so the
            # session opens and the agent is told at the point it tries to do
            # something. Nothing else here has a side effect to prevent.
            logger.warning(f"{method} on {host} carries no known sandbox marker")
            self._publish(
                protocol.event(
                    protocol.Event.UNKNOWN_SANDBOX,
                    host=host,
                    path=flow.request.path,
                    method=method,
                    tool=call.tool if call is not None else None,
                )
            )
            if call is None:
                return
            flow.response = refusal(call, "Refused: request carries no known sandbox marker.")
            return

        if call is None:
            # initialize, tools/list, notifications — no side effects, and
            # blocking them would only break the connection.
            self._publish(
                protocol.event(
                    protocol.Event.PASSTHROUGH, host=host, sandbox=sandbox, method=method
                )
            )
            return

        verdict = await self._gate.ask(sandbox, host, call.tool, call.arguments)
        if not verdict.allowed:
            reason = REFUSAL.get(verdict.refused_by, "Refused by the gate: {tool}")
            flow.response = refusal(call, reason.format(tool=call.tool))

    def responseheaders(self, flow: http.HTTPFlow) -> None:
        """Hand the response body straight through, unbuffered.

        Set before any of it arrives, so an event-stream that never ends is a
        stream and not a memory leak.
        """
        if self._gated(flow) and flow.response is not None:
            flow.response.stream = True
