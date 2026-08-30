"""Policy that reloads itself, and what mitmproxy is told to intercept."""

import json

import pytest
from daemon.config import ConfigError, Policy, PolicyFile


def write_policy(path, **overrides):
    body = {"hosts": ["mcp.linear.app"], "sandboxes": ["sbx"], "timeout": 300}
    body.update(overrides)
    path.write_text(json.dumps(body), "utf-8")


def test_an_edit_takes_effect_without_a_restart(tmp_path):
    path = tmp_path / "policy.json"
    write_policy(path)
    policy = PolicyFile(path)
    assert policy.load().hosts == ("mcp.linear.app",)

    write_policy(path, hosts=["mcp.linear.app", "api.githubcopilot.com"])
    assert policy.load().hosts == ("mcp.linear.app", "api.githubcopilot.com")


def test_an_unchanged_file_is_not_reparsed(tmp_path):
    path = tmp_path / "policy.json"
    write_policy(path)
    policy = PolicyFile(path)
    first = policy.load()
    assert policy.load() is first  # same object: no parse, no allocation


def test_a_broken_edit_keeps_the_last_good_policy(tmp_path, caplog):
    """Mid-edit garbage must not gate nothing, nor refuse everything."""
    path = tmp_path / "policy.json"
    write_policy(path)
    policy = PolicyFile(path)
    good = policy.load()

    path.write_text("{ half-typed", "utf-8")
    with caplog.at_level("WARNING"):
        assert policy.load() == good
    assert len(caplog.records) == 1

    write_policy(path, hosts=["example.test"])
    assert policy.load().hosts == ("example.test",)


def test_a_policy_that_would_gate_nothing_is_refused(tmp_path):
    path = tmp_path / "policy.json"
    write_policy(path, hosts=[])
    with pytest.raises(ConfigError, match="hosts"):
        Policy.load(path)


def test_only_the_gated_hosts_are_intercepted():
    """Everything else is tunnelled: no TLS termination, no pinning trouble."""
    import re

    from daemon.main import allow_hosts

    patterns = allow_hosts(("mcp.linear.app",))
    assert any(re.match(p, "mcp.linear.app:443") for p in patterns)
    # A dot is a dot, not any character — mcpxlinear.app must not match.
    assert not any(re.match(p, "mcpxlinear.app:443") for p in patterns)
    # And a suffix match is not enough: only the host itself.
    assert not any(re.match(p, "evil.com:443") for p in patterns)


def test_a_reload_reaches_whoever_asked_to_be_told(tmp_path):
    """What mitmproxy intercepts is wired up through this callback."""
    path = tmp_path / "policy.json"
    seen = []
    write_policy(path)
    policy = PolicyFile(path, on_reload=lambda policy: seen.append(policy.hosts))
    policy.load()
    assert seen == [("mcp.linear.app",)]

    policy.load()  # unchanged file: no reload, no callback
    assert len(seen) == 1

    write_policy(path, hosts=["mcp.linear.app", "api.githubcopilot.com"])
    policy.load()
    assert seen[-1] == ("mcp.linear.app", "api.githubcopilot.com")
