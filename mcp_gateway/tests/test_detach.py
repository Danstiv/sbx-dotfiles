"""Background start and stop, driven the way a person would drive them.

Nothing is stubbed: `main(["-d"])` really starts a second process, and the
assertions are the same ones the operator makes — does the control port answer,
and does it stop answering.
"""

import json
import socket
import time
from pathlib import Path

from daemon.config import Config
from daemon.main import main

from .test_e2e import free_port


def write_config(root: Path) -> Config:
    config = Config(
        root=root,
        proxy_port=free_port(),
        control_port=free_port(),
        mitmproxy_config_dir=root / "mitm",
    )
    (root / "config.json").write_text(json.dumps({
        "proxy": {"port": config.proxy_port},
        "control": {"port": config.control_port},
        "mitmproxy-config-dir": str(config.mitmproxy_config_dir),
    }), "utf-8")
    config.policy_path.write_text(json.dumps({
        "hosts": ["localhost"],
        "sandboxes": ["sbx"],
    }), "utf-8")
    return config


def answering(config: Config) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", config.control_port), 0.3):
            return True
    except OSError:
        return False


def test_start_and_stop(tmp_path):
    config = write_config(tmp_path)
    argv = ["--config", str(tmp_path / "config.json")]

    assert main([*argv, "--status"]) == 1  # nothing there yet
    try:
        assert main([*argv, "-d"]) == 0
        assert answering(config)
        assert main([*argv, "--status"]) == 0
        assert config.pid_path.exists()
        # Started detached, so it must have outlived the call that started it.
        assert main([*argv, "-d"]) == 0  # idempotent, not a second copy
    finally:
        assert main([*argv, "--stop"]) == 0

    assert not answering(config)
    assert not config.pid_path.exists()
    assert main([*argv, "--stop"]) == 0  # stopping a stopped daemon is fine


def test_start_reports_a_daemon_that_dies(tmp_path, capsys):
    config = write_config(tmp_path)
    config.policy_path.write_text(json.dumps({"hosts": [], "sandboxes": []}), "utf-8")

    assert main(["--config", str(tmp_path / "config.json"), "-d"]) == 1
    # The reason is in the log, and the log's tail is on stderr.
    assert "hosts" in capsys.readouterr().err
    assert not config.pid_path.exists()


def test_stop_clears_a_stale_pidfile(tmp_path):
    config = write_config(tmp_path)
    config.pid_path.write_text("999999\n", "utf-8")

    assert main(["--config", str(tmp_path / "config.json"), "--stop"]) == 0
    assert not config.pid_path.exists()
