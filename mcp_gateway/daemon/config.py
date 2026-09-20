"""Everything the daemon reads and writes.

The user writes two files: config.json, the ports and paths, read once; and
policy.json, what to gate and how, re-read whenever it changes. The daemon
writes three: decisions.json for remembered "always" answers, secret.txt for
the control-socket token, and audit.jsonl for every call held and every
decision taken.

Config is what the process bound to at startup; policy is consulted per call,
so it can be re-read per call. Authored and generated files stay apart so that
writing one never rewrites the other.
"""

import json
import logging
import os
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# A body this size is refused rather than inspected: the gate has to parse what
# it approves, and waving through what it cannot read defeats the point.
MAX_BODY = 1_000_000

logger = logging.getLogger(__name__)


class ConfigError(Exception):
    """Raised for a config file that cannot be honoured as written."""


def _expand(value: str) -> Path:
    return Path(value).expanduser()


def _read_json_object(path: Path, remedy: str) -> dict[str, Any]:
    try:
        raw = json.loads(path.read_text("utf-8"))
    except FileNotFoundError:
        raise ConfigError(f"nothing at {path} — {remedy}") from None
    except json.JSONDecodeError as exc:
        raise ConfigError(f"{path} is not valid JSON: {exc}") from None
    except OSError as exc:
        raise ConfigError(f"cannot read {path}: {exc}") from None
    if not isinstance(raw, dict):
        raise ConfigError(f"{path} must contain a JSON object")
    return raw


def _string_list(raw: dict[str, Any], key: str) -> tuple[str, ...]:
    value = raw.get(key) or []
    if not isinstance(value, list) or any(not isinstance(v, str) for v in value):
        raise ConfigError(f'"{key}" must be a list of strings')
    return tuple(value)


@dataclass(frozen=True)
class Policy:
    """What to gate and how. Re-read whenever policy.json changes."""

    # Hosts to gate. Traffic to anything else is not touched.
    hosts: tuple[str, ...] = ()

    # Sandbox names accepted in the X-Sbx-Sandbox header. Also a whitelist: a
    # name that is not listed is not recognised, and the call is refused.
    sandboxes: tuple[str, ...] = ()

    # Tools that are confirmed every time; "always" is never offered for them,
    # because one name covers unbounded behaviour.
    always_ask: tuple[str, ...] = ()

    timeout: float = 300.0

    @classmethod
    def load(cls, path: Path) -> Policy:
        raw = _read_json_object(path, "copy examples/policy.json")
        policy = cls(
            hosts=_string_list(raw, "hosts"),
            sandboxes=_string_list(raw, "sandboxes"),
            always_ask=_string_list(raw, "always_ask"),
            timeout=float(raw["timeout"]) if raw.get("timeout") is not None else 300.0,
        )
        if not policy.hosts:
            raise ConfigError('"hosts" is empty — the gate would inspect nothing')
        if not policy.sandboxes:
            raise ConfigError('"sandboxes" is empty — every call would be refused')
        return policy

    def offers_always(self, tool: str) -> bool:
        return tool not in self.always_ask


@dataclass
class PolicyFile:
    """The policy, re-read when the file changes.

    A stat is cheap and a parse is not, so only a moved mtime or size triggers
    one. An edit that will not parse keeps the last good policy: an empty one
    would either gate nothing or refuse everything.
    """

    path: Path
    on_reload: Callable[[Policy], None] | None = None
    _policy: Policy | None = field(default=None, repr=False)
    _stamp: tuple[float, int] | None = field(default=None, repr=False)
    _complained: bool = field(default=False, repr=False)

    def _current_stamp(self) -> tuple[float, int] | None:
        try:
            info = self.path.stat()
        except OSError:
            return None
        return (info.st_mtime, info.st_size)

    def load(self) -> Policy:
        stamp = self._current_stamp()
        if self._policy is not None and stamp == self._stamp:
            return self._policy
        try:
            policy = Policy.load(self.path)
        except ConfigError as exc:
            if self._policy is None:
                raise
            if not self._complained:
                self._complained = True
                logger.warning(f"{self.path} is unusable ({exc}); keeping the last policy")
            self._stamp = stamp
            return self._policy
        self._complained = False
        self._stamp = stamp
        first, self._policy = self._policy is None, policy
        if not first:
            logger.info(f"reloaded {self.path.name}")
        if self.on_reload is not None:
            self.on_reload(policy)
        return policy


@dataclass(frozen=True)
class Config:
    """The wiring. Read once, because the process binds to it at startup."""

    root: Path
    proxy_host: str = "127.0.0.1"
    proxy_port: int = 8080
    control_host: str = "127.0.0.1"
    control_port: int = 8765

    # Unset, mitmproxy uses ~/.mitmproxy — the CA already trusted on the host.
    mitmproxy_config_dir: Path | None = None

    # Extra CA bundle for verifying gated hosts, used by the tests to trust
    # their throwaway upstream. Verification is never off; this only adds to
    # what the system already trusts.
    upstream_ca: Path | None = None

    @property
    def policy_path(self) -> Path:
        return self.root / "policy.json"

    @property
    def decisions_path(self) -> Path:
        return self.root / "decisions.json"

    @property
    def secret_path(self) -> Path:
        return self.root / "secret.txt"

    @property
    def audit_path(self) -> Path:
        return self.root / "audit.jsonl"

    @property
    def tokens_path(self) -> Path:
        return self.root / "tokens.json"

    @property
    def pid_path(self) -> Path:
        return self.root / "daemon.pid"

    @property
    def log_path(self) -> Path:
        return self.root / "daemon.log"

    @classmethod
    def load(cls, path: Path) -> Config:
        raw = _read_json_object(path, "copy examples/config.json")
        proxy = raw.get("proxy") or {}
        control = raw.get("control") or {}

        def if_set(value: Any, cast: Callable[[Any], Any]) -> Any:
            return None if value is None else cast(value)

        # Only what the file actually sets is passed on, so the field defaults
        # above stay the single source of truth for everything it omits.
        settings = {
            "proxy_host": proxy.get("host"),
            "proxy_port": if_set(proxy.get("port"), int),
            "control_host": control.get("host"),
            "control_port": if_set(control.get("port"), int),
            "mitmproxy_config_dir": if_set(raw.get("mitmproxy-config-dir"), _expand),
            "upstream_ca": if_set(raw.get("upstream-ca"), _expand),
        }
        return cls(
            root=path.parent,
            **{key: value for key, value in settings.items() if value is not None},
        )


def read_tokens(path: Path) -> dict[str, str]:
    """Bearer tokens to attach to gated hosts, keyed by host.

    Read once, at startup, unlike the policy: a credential that changed under
    a running daemon would be one more moment where it is on disk being read.
    Absent, nothing is attached and the hosts authenticate themselves.
    """
    if not path.exists():
        return {}
    raw = _read_json_object(path, "see the README")
    for host, token in raw.items():
        if not isinstance(token, str):
            raise ConfigError(f"{path}: the value for {host} must be a token string")
    return raw


def read_secret(path: Path) -> str:
    """Return the control-socket secret, generating it on first run.

    A real credential: whoever reads it can approve the agent's tool calls.
    The 0600 is best effort — Windows ignores POSIX modes, and refusing to
    start over that would refuse to start at all.
    """
    try:
        existing = path.read_text("utf-8").strip()
        if existing:
            return existing
    except FileNotFoundError:
        pass
    token = secrets.token_urlsafe(32)
    path.write_text(token + "\n", "utf-8")
    try:
        path.chmod(0o600)
    except OSError:
        pass
    return token


# ------------------------------------------------------------------- storage

def _write_atomic(path: Path, text: str) -> None:
    """Replace `path` in one step, so a crash mid-write cannot truncate it.

    os.replace is atomic on both POSIX and Windows.
    """
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, "utf-8")
    os.replace(tmp, path)


@dataclass
class DecisionStore:
    """Stored answers, keyed by (sandbox, host, tool).

    The host is part of the key on purpose. Tool names are not unique across
    MCP servers — `get_issue` exists in both Linear and GitHub — so keying on
    the name alone would let an answer given about one server silently apply to
    another.
    """

    path: Path
    audit_path: Path | None = None
    _warned: bool = field(default=False, repr=False)

    def _read(self) -> dict[str, Any]:
        try:
            data = json.loads(self.path.read_text("utf-8"))
        except FileNotFoundError:
            return {}
        except Exception as exc:
            # Never fail open on an unreadable store: no stored permissions
            # means everything gets asked about, which is the safe direction.
            # Worth saying out loud, though — silence here looks exactly like
            # every "always" having been forgotten. Once per spell of trouble,
            # because this runs on every gated call.
            if not self._warned:
                self._warned = True
                logger.warning(f"cannot read {self.path} ({exc}); asking about everything")
            return {}
        self._warned = False
        return data if isinstance(data, dict) else {}

    def allowed(self, sandbox: str, host: str, tool: str) -> bool:
        entry = self._read().get(sandbox, {}).get(host, {}).get(tool)
        return entry == "allow"

    def remember(self, sandbox: str, host: str, tool: str) -> None:
        """Record an "always" answer, merging into whatever is on disk now.

        Re-reading immediately before the write is what keeps edits made by
        hand, while the daemon is up, from being overwritten.
        """
        data = self._read()
        data.setdefault(sandbox, {}).setdefault(host, {})[tool] = "allow"
        _write_atomic(self.path, json.dumps(data, indent=2, sort_keys=True, ensure_ascii=False))

    def forget(self, sandbox: str, host: str, tool: str | None = None) -> int:
        """Drop one stored answer, or every answer for a sandbox/host pair."""
        data = self._read()
        hosts = data.get(sandbox)
        if not isinstance(hosts, dict) or host not in hosts:
            return 0
        tools = hosts[host]
        if tool is None:
            removed = len(tools)
            del hosts[host]
        else:
            removed = 1 if tools.pop(tool, None) is not None else 0
            if not tools:
                del hosts[host]
        if not hosts:
            del data[sandbox]
        if removed:
            _write_atomic(self.path, json.dumps(data, indent=2, sort_keys=True, ensure_ascii=False))
        return removed

    def entries(self) -> list[dict[str, str]]:
        return [
            {"sandbox": sandbox, "host": host, "tool": tool}
            for sandbox, hosts in sorted(self._read().items())
            if isinstance(hosts, dict)
            for host, tools in sorted(hosts.items())
            if isinstance(tools, dict)
            for tool in sorted(tools)
        ]

    def audit(self, **fields: Any) -> None:
        """Append one line to the audit log. Never raises."""
        if self.audit_path is None:
            return
        line = json.dumps({"at": time.time(), **fields}, ensure_ascii=False)
        try:
            with self.audit_path.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")
        except OSError:
            pass
