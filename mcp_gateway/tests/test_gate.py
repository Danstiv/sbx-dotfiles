"""The holding pen, exercised without a proxy or a socket in sight."""

import asyncio

import protocol
import pytest
from daemon.config import Config, DecisionStore, Policy
from daemon.gate import Gate


class _Fixed:
    """A PolicyFile that never changes, so these tests need no file at all."""

    def __init__(self, policy: Policy) -> None:
        self._policy = policy

    def load(self) -> Policy:
        return self._policy


@pytest.fixture
def make_gate(tmp_path):
    def build(timeout: float = 5.0, always_ask: tuple[str, ...] = ()):
        config = Config(root=tmp_path)
        policy = _Fixed(Policy(
            hosts=("mcp.linear.app",),
            sandboxes=("sbx",),
            always_ask=always_ask,
            timeout=timeout,
        ))
        store = DecisionStore(config.decisions_path, config.audit_path)
        published: list[dict] = []
        return Gate(policy, store, published.append), store, published

    return build


async def _ask(gate: Gate, tool: str = "save_issue"):
    """Start an ask() and let it register before the test inspects the gate."""
    task = asyncio.create_task(gate.ask("sbx", "mcp.linear.app", tool, {"a": 1}))
    await asyncio.sleep(0)
    return task


async def test_first_answer_wins_and_is_broadcast(make_gate):
    gate, _, published = make_gate()
    task = await _ask(gate)
    request_id = published[0]["request"]["id"]

    assert gate.decide(request_id, "once") is True
    # A second client answering the same prompt is told it is settled rather
    # than being allowed to overturn it.
    assert gate.decide(request_id, "deny") is False

    assert (await task).allowed is True
    assert protocol.resolved(request_id, "once", "client") in published


async def test_always_is_stored_and_short_circuits(make_gate):
    gate, store, published = make_gate()
    task = await _ask(gate)
    gate.decide(published[0]["request"]["id"], "always")
    assert (await task).allowed is True
    assert store.allowed("sbx", "mcp.linear.app", "save_issue")

    published.clear()
    # Second time round nothing is held: it resolves without a prompt.
    assert (await gate.ask("sbx", "mcp.linear.app", "save_issue", {})).allowed is True
    assert gate.snapshot() == []
    assert published[0]["kind"] == protocol.Event.AUTO_ALLOWED


async def test_always_covers_the_rest_of_the_batch(make_gate):
    """An agent fires a batch at once, so all of it is held before the first
    answer. The calls differ only in their arguments — `issue_read` with `get`
    beside `issue_read` with `get_comments` — and one "always" settles them all
    rather than asking the same question again for each.
    """
    gate, _, published = make_gate()
    batch = [
        asyncio.create_task(
            gate.ask("sbx", "mcp.linear.app", "issue_read", {"method": method})
        )
        for method in ("get", "get_comments")
    ]
    elsewhere = asyncio.create_task(gate.ask("sbx", "mcp.linear.app", "save_issue", {}))
    await asyncio.sleep(0)
    ids = [message["request"]["id"] for message in published]

    assert gate.decide(ids[0], "always") is True

    assert [(await task).allowed for task in batch] == [True, True]
    # Only what the new rule covers: a different tool keeps waiting for its own
    # answer instead of being swept along with the batch.
    assert [item["tool"] for item in gate.snapshot()] == ["save_issue"]
    assert protocol.resolved(ids[1], "once", "remembered") in published

    elsewhere.cancel()
    with pytest.raises(asyncio.CancelledError):
        await elsewhere


async def test_stored_answer_is_scoped_to_its_host(make_gate):
    """`get_issue` exists in more than one MCP server; one yes is not both."""
    gate, store, _ = make_gate()
    store.remember("sbx", "mcp.linear.app", "get_issue")
    assert store.allowed("sbx", "mcp.linear.app", "get_issue")
    assert not store.allowed("sbx", "api.githubcopilot.com", "get_issue")
    assert not store.allowed("other", "mcp.linear.app", "get_issue")


async def test_an_unreadable_store_asks_about_everything_and_says_so(make_gate, caplog):
    """A corrupt file must not look like every "always" being forgotten."""
    gate, store, _ = make_gate()
    store.remember("sbx", "mcp.linear.app", "list_teams")
    store.path.write_text("{ this is not json", "utf-8")

    with caplog.at_level("WARNING"):
        assert not store.allowed("sbx", "mcp.linear.app", "list_teams")
        assert not store.allowed("sbx", "mcp.linear.app", "list_teams")
    # Warned, but only once: this runs on every gated call.
    assert len(caplog.records) == 1

    store.path.write_text("{}", "utf-8")
    assert not store.allowed("sbx", "mcp.linear.app", "list_teams")
    with caplog.at_level("WARNING"):
        store.path.write_text("{ broken again", "utf-8")
        assert not store.allowed("sbx", "mcp.linear.app", "list_teams")
    assert len(caplog.records) == 2


async def test_always_ask_tools_never_offer_always(make_gate):
    gate, store, published = make_gate(always_ask=("bash",))
    task = await _ask(gate, "bash")
    assert published[0]["request"]["offer_always"] is False

    # Even if a client sends "always" anyway, it downgrades to a single pass
    # and nothing is written down.
    gate.decide(published[0]["request"]["id"], "always")
    assert (await task).allowed is True
    assert not store.allowed("sbx", "mcp.linear.app", "bash")


async def test_timeout_denies_and_says_so(make_gate):
    gate, _, published = make_gate(timeout=0.05)
    # Refused *by the timeout* — the caller has to be able to tell that from a
    # refusal the user actually gave.
    assert (await gate.ask("sbx", "mcp.linear.app", "save_issue", {})).refused_by == (
        protocol.Source.TIMEOUT
    )
    assert gate.snapshot() == []
    assert published[-1] == {
        "type": protocol.Message.RESOLVED,
        "id": published[0]["request"]["id"],
        "decision": "deny",
        "source": "timeout",
    }


async def test_pending_survives_with_no_clients_and_appears_in_snapshot(make_gate):
    """Nobody is listening — the call is still held, not dropped."""
    gate, _, published = make_gate()
    task = await _ask(gate)
    assert [item["tool"] for item in gate.snapshot()] == ["save_issue"]

    # A client attaching now is handed the backlog.
    assert protocol.hello(gate.snapshot())["pending"][0]["id"] == published[0]["request"]["id"]
    gate.decide(gate.snapshot()[0]["id"], "deny")
    assert (await task).refused_by == protocol.Source.CLIENT


async def test_cancelled_call_is_not_forwarded(make_gate):
    """The agent gave up: releasing the call now would run it behind its back."""
    gate, _, published = make_gate()
    task = await _ask(gate)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert gate.snapshot() == []
    assert published[-1]["source"] == "cancelled"
