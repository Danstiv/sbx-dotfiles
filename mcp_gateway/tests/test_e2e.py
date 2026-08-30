"""The whole chain, end to end: proxy, TLS, gate, control socket.

A client speaks HTTPS through the real proxy to a real (throwaway) server, and
a websocket client stands in for the console. Nothing is mocked between them.
"""

import asyncio
import json
import socket
from pathlib import Path

import httpx
import protocol
import pytest
import websockets
from daemon.main import run
from daemon.config import MAX_BODY, Config

from .fake_mcp import FakeMCP, issue_certs

SECRET = "test-secret"
TOKEN = "s3cret-token"


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


async def wait_for_port(port: int, timeout: float = 15.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        try:
            _, writer = await asyncio.open_connection("127.0.0.1", port)
            writer.close()
            return
        except OSError:
            await asyncio.sleep(0.05)
    raise TimeoutError(f"nothing listening on {port}")


async def wait_for_file(path: Path, timeout: float = 15.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if path.exists():
            return
        await asyncio.sleep(0.05)
    raise TimeoutError(f"{path} never appeared")


class Operator:
    """A minimal control-socket client — what the console does, without a TTY."""

    def __init__(self, ws) -> None:
        self.ws = ws

    async def next_of(self, *types: str) -> dict:
        async for raw in self.ws:
            message = json.loads(raw)
            if message["type"] in types:
                return message
        raise AssertionError("control socket closed")

    async def answer(self, request_id: str, decision: str) -> None:
        await self.ws.send(json.dumps(protocol.decide(request_id, decision)))


@pytest.fixture
async def stand(tmp_path, request):
    """Proxy + gate + control socket in front of a real HTTPS server.

    Parametrise indirectly with a number to shorten the hold timeout, for
    tests that want a prompt to expire rather than be answered, or with
    "with-token" to give the daemon a token to attach.
    """
    param = getattr(request, "param", None)
    hold = param if isinstance(param, (int, float)) else 10.0
    ca_pem, server_pem = issue_certs(tmp_path)
    if param == "with-token":
        (tmp_path / "tokens.json").write_text(
            json.dumps({"localhost": TOKEN}), "utf-8"
        )
    with FakeMCP(server_pem) as upstream:
        mitm_dir = tmp_path / "mitm"
        config = Config(
            root=tmp_path,
            proxy_port=free_port(),
            control_port=free_port(),
            mitmproxy_config_dir=mitm_dir,
            upstream_ca=ca_pem,
        )
        config.policy_path.write_text(json.dumps({
            "hosts": ["localhost"],
            "sandboxes": ["sbx"],
            "always_ask": ["bash"],
            "timeout": hold,
        }), "utf-8")
        task = asyncio.create_task(run(config, SECRET))
        try:
            await wait_for_port(config.proxy_port)
            await wait_for_port(config.control_port)
            await wait_for_file(mitm_dir / "mitmproxy-ca-cert.pem")

            client = httpx.AsyncClient(
                proxy=f"http://127.0.0.1:{config.proxy_port}",
                verify=str(mitm_dir / "mitmproxy-ca-cert.pem"),
                timeout=20.0,
            )
            async with client:
                async with websockets.connect(
                    f"ws://127.0.0.1:{config.control_port}",
                    additional_headers={"Authorization": f"Bearer {SECRET}"},
                ) as ws:
                    operator = Operator(ws)
                    assert (await operator.next_of(protocol.Message.HELLO))["pending"] == []
                    yield config, upstream, client, operator
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


def call(tool: str, **arguments) -> dict:
    return {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": tool, "arguments": arguments},
    }


async def post(client: httpx.AsyncClient, config: Config, port: int, body: dict, path: str):
    return await client.post(f"https://localhost:{port}{path}", json=body)


async def test_allowed_call_reaches_upstream_with_the_suffix_stripped(stand):
    config, upstream, client, operator = stand
    pending = asyncio.create_task(
        post(client, config, upstream.port, call("save_issue", title="hi"), "/mcp-sbx")
    )

    held = await operator.next_of(protocol.Message.REQUEST)
    assert held["request"]["sandbox"] == "sbx"
    assert held["request"]["tool"] == "save_issue"
    assert held["request"]["arguments"] == {"title": "hi"}
    await operator.answer(held["request"]["id"], "once")

    response = await pending
    assert response.status_code == 200
    assert response.json()["result"]["content"][0]["text"] == "did the thing"
    # The sandbox suffix is a routing marker, not something upstream should see.
    assert upstream.paths == ["/mcp"]


async def test_denied_call_never_reaches_upstream(stand):
    config, upstream, client, operator = stand
    pending = asyncio.create_task(
        post(client, config, upstream.port, call("save_issue", title="no"), "/mcp-sbx")
    )

    held = await operator.next_of(protocol.Message.REQUEST)
    await operator.answer(held["request"]["id"], "deny")

    response = await pending
    # A refusal is a healthy 200 carrying isError, so the agent sees one failed
    # tool instead of a broken server.
    assert response.status_code == 200
    assert response.json()["result"]["isError"] is True
    assert upstream.seen == []


@pytest.mark.parametrize("stand", [0.4], indirect=True)
async def test_an_expired_call_does_not_blame_the_user(stand):
    """Caught live: nobody was at the console, and the agent was told it had
    been denied. "I said no" and "I was away" call for different next steps."""
    config, upstream, client, operator = stand
    body = call("save_issue", title="unattended")
    response = await post(client, config, upstream.port, body, "/mcp-sbx")

    text = response.json()["result"]["content"][0]["text"]
    assert response.json()["result"]["isError"] is True
    assert "The user did not answer" in text
    assert "Denied by the user" not in text
    assert upstream.seen == []


async def test_always_is_remembered_and_the_next_call_is_not_held(stand):
    config, upstream, client, operator = stand
    pending = asyncio.create_task(
        post(client, config, upstream.port, call("list_teams"), "/mcp-sbx")
    )
    held = await operator.next_of(protocol.Message.REQUEST)
    await operator.answer(held["request"]["id"], "always")
    assert (await pending).status_code == 200

    second = asyncio.create_task(
        post(client, config, upstream.port, call("list_teams"), "/mcp-sbx")
    )
    event = await operator.next_of(protocol.Message.EVENT)
    assert event["kind"] == protocol.Event.AUTO_ALLOWED
    assert (await second).status_code == 200
    assert len(upstream.seen) == 2

    stored = json.loads(config.decisions_path.read_text("utf-8"))
    assert stored == {"sbx": {"localhost": {"list_teams": "allow"}}}


async def test_a_forgotten_sandbox_shows_up_at_initialize(stand):
    """The failure this catches: `sbx mcp load` reports success, the session
    never opens, and nothing says why. It shows up on the first method of the
    session, not on the first tool call that session never gets to make."""
    config, upstream, client, operator = stand
    body = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}
    response = await post(client, config, upstream.port, body, "/mcp-forgotten")

    event = await operator.next_of(protocol.Message.EVENT)
    assert event["kind"] == protocol.Event.UNKNOWN_SANDBOX
    assert event["method"] == "initialize"
    assert event["tool"] is None

    # Forwarded, not refused: with the suffix unrecognised the path was never
    # rewritten, so upstream turns it down by itself.
    assert response.status_code == 404
    assert upstream.paths == ["/mcp-forgotten"]


async def test_unknown_sandbox_suffix_is_refused_and_announced(stand):
    config, upstream, client, operator = stand
    response = await post(client, config, upstream.port, call("save_issue"), "/mcp-nope")

    assert response.json()["result"]["isError"] is True
    assert upstream.seen == []
    event = await operator.next_of(protocol.Message.EVENT)
    assert event["kind"] == protocol.Event.UNKNOWN_SANDBOX


async def test_a_json_rpc_batch_is_refused(stand):
    """MCP dropped batching in 2025-06-18; an array must not sail through."""
    config, upstream, client, operator = stand
    response = await post(client, config, upstream.port, [call("save_issue")], "/mcp-sbx")

    assert response.status_code == 200
    # INVALID_REQUEST: a batch really is not a valid MCP request object.
    assert response.json()["error"] == {
        "code": -32600,
        "message": "Refused: JSON-RPC batching is not supported.",
    }
    assert upstream.seen == []


async def test_an_oversized_body_is_refused_unparsed(stand):
    """Too big to inspect is too big to vouch for."""
    config, upstream, client, operator = stand
    body = call("save_issue", title="x" * (MAX_BODY + 1))
    response = await post(client, config, upstream.port, body, "/mcp-sbx")

    assert response.status_code == 200
    # A server error, not INVALID_REQUEST: nothing was parsed, so the request
    # was never shown to be malformed — the gate simply would not look.
    assert response.json()["error"]["code"] == -32000
    assert response.json()["id"] is None
    assert upstream.seen == []


async def test_non_tool_traffic_is_never_held(stand):
    config, upstream, client, operator = stand
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
    response = await post(client, config, upstream.port, body, "/mcp-sbx")

    assert response.status_code == 200
    assert upstream.seen == [body]
    event = await operator.next_of(protocol.Message.EVENT)
    assert event["kind"] == protocol.Event.PASSTHROUGH


async def test_a_second_operator_is_told_who_answered(stand):
    config, upstream, client, operator = stand
    async with websockets.connect(
        f"ws://127.0.0.1:{config.control_port}",
        additional_headers={"Authorization": f"Bearer {SECRET}"},
    ) as other_ws:
        other = Operator(other_ws)
        await other.next_of(protocol.Message.HELLO)

        pending = asyncio.create_task(
            post(client, config, upstream.port, call("save_issue"), "/mcp-sbx")
        )
        held = await operator.next_of(protocol.Message.REQUEST)
        assert (await other.next_of(protocol.Message.REQUEST))["request"]["id"] == held["request"]["id"]

        await operator.answer(held["request"]["id"], "once")
        settled = await other.next_of(protocol.Message.RESOLVED)
        assert settled == protocol.resolved(held["request"]["id"], "once", "client")
        await pending


async def test_a_late_operator_is_handed_the_backlog(stand):
    """The call is held with nobody watching; attaching replays it."""
    config, upstream, client, operator = stand
    pending = asyncio.create_task(
        post(client, config, upstream.port, call("save_issue"), "/mcp-sbx")
    )
    held = await operator.next_of(protocol.Message.REQUEST)

    async with websockets.connect(
        f"ws://127.0.0.1:{config.control_port}",
        additional_headers={"Authorization": f"Bearer {SECRET}"},
    ) as late_ws:
        late = Operator(late_ws)
        hello = await late.next_of(protocol.Message.HELLO)
        assert [item["id"] for item in hello["pending"]] == [held["request"]["id"]]
        await late.answer(held["request"]["id"], "deny")

    assert (await pending).json()["result"]["isError"] is True


async def test_dropping_the_socket_does_not_release_the_call(stand):
    """The prompt outlives the connection that displayed it."""
    config, upstream, client, operator = stand
    pending = asyncio.create_task(
        post(client, config, upstream.port, call("save_issue"), "/mcp-sbx")
    )
    held = await operator.next_of(protocol.Message.REQUEST)
    await operator.ws.close()

    assert not pending.done()
    async with websockets.connect(
        f"ws://127.0.0.1:{config.control_port}",
        additional_headers={"Authorization": f"Bearer {SECRET}"},
    ) as ws:
        again = Operator(ws)
        hello = await again.next_of(protocol.Message.HELLO)
        assert [item["id"] for item in hello["pending"]] == [held["request"]["id"]]
        await again.answer(held["request"]["id"], "once")

    assert (await pending).status_code == 200
    assert upstream.seen != []


async def test_event_streams_are_relayed_as_they_arrive(stand):
    """MCP answers over SSE and may hold the stream open for minutes.

    Anything that waits for the body to finish before passing it on is a hang,
    so the first event has to reach the client while the server is still
    inside the response — here, blocked until the test releases it.
    """
    config, upstream, client, operator = stand
    body = call("stream_me")
    request = client.build_request(
        "POST", f"https://localhost:{upstream.port}/mcp-sbx", json=body
    )
    # send() waits for response headers, which will not come until the call is
    # approved — so it has to be in flight before the answer goes out.
    pending = asyncio.create_task(client.send(request, stream=True))

    held = await operator.next_of(protocol.Message.REQUEST)
    await operator.answer(held["request"]["id"], "once")

    response = await pending
    events = response.aiter_lines()
    first = await asyncio.wait_for(anext(events), timeout=10)
    assert first == "data: one"

    # Proof it was not buffered: the server is still blocked in the middle of
    # the response, and the first event has already arrived.
    upstream.release.set()
    rest = [line async for line in events]
    assert "data: two" in rest
    await response.aclose()


async def test_the_control_socket_needs_the_secret(stand):
    config, *_ = stand
    with pytest.raises(websockets.InvalidStatus) as caught:
        async with websockets.connect(
            f"ws://127.0.0.1:{config.control_port}",
            additional_headers={"Authorization": "Bearer wrong"},
        ):
            pass
    assert caught.value.response.status_code == 401


@pytest.mark.parametrize("stand", ["with-token"], indirect=True)
async def test_a_token_is_attached_for_a_known_sandbox(stand):
    """The credential lives on the host and is added on the way out."""
    config, upstream, client, operator = stand
    pending = asyncio.create_task(
        post(client, config, upstream.port, call("save_issue"), "/mcp-sbx")
    )
    held = await operator.next_of(protocol.Message.REQUEST)
    await operator.answer(held["request"]["id"], "once")
    assert (await pending).status_code == 200

    assert upstream.authorization == [f"Bearer {TOKEN}"]
    # And it stays out of everything anyone else can read.
    assert TOKEN not in json.dumps(held)
    assert TOKEN not in config.audit_path.read_text("utf-8")


@pytest.mark.parametrize("stand", ["with-token"], indirect=True)
async def test_no_token_without_a_known_sandbox(stand):
    """An anonymous caller is not one to lend a credential to."""
    config, upstream, client, operator = stand
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
    await post(client, config, upstream.port, body, "/mcp")

    assert upstream.authorization == [None]


async def test_nothing_is_attached_when_no_token_is_configured(stand):
    config, upstream, client, operator = stand
    pending = asyncio.create_task(
        post(client, config, upstream.port, call("save_issue"), "/mcp-sbx")
    )
    held = await operator.next_of(protocol.Message.REQUEST)
    await operator.answer(held["request"]["id"], "once")
    await pending

    assert upstream.authorization == [None]
