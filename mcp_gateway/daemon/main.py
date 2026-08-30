import argparse
import asyncio
import logging
import re
import sys
from pathlib import Path

from mitmproxy import options
from mitmproxy.tools.dump import DumpMaster

from .addon import GateAddon
from .config import (
    Config,
    ConfigError,
    DecisionStore,
    Policy,
    PolicyFile,
    read_secret,
    read_tokens,
)
from .control import ControlServer

logger = logging.getLogger(__name__)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="mcp-gateway")
    parser.add_argument(
        "--config",
        type=Path,
        default=Path(__file__).resolve().parent.parent / "data" / "config.json",
        help="path to config.json (state files live beside it)",
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    return parser


async def watch_policy(policy: PolicyFile, interval: float = 2.0) -> None:
    """Notice policy edits even while no traffic is arriving.

    Traffic alone does for the gate, which reads the policy per call. It does
    not do for allow_hosts, which mitmproxy consults when a connection opens,
    before any of this sees it: a newly added host would not be intercepted,
    so no flow would reach the addon, so nothing would prompt the re-read that
    would have allowed it. That circle only breaks from outside.
    """
    while True:
        await asyncio.sleep(interval)
        try:
            policy.load()
        except Exception:  # never let a bad edit stop the watch
            logger.exception("while re-reading the policy")


def allow_hosts(hosts: tuple[str, ...]) -> list[str]:
    """mitmproxy patterns for the only hosts worth terminating TLS on.
    Everything else is tunnelled through untouched.
    """
    return [rf"^{re.escape(host)}:\d+$" for host in hosts]


async def run(config: Config, secret: str) -> None:
    store = DecisionStore(config.decisions_path, config.audit_path)

    opts = options.Options(
        listen_host=config.proxy_host,
        listen_port=config.proxy_port,
        # Upstream certificates are verified normally. A gate that accepted any
        # certificate on the way out would be the weakest link in the chain it
        # exists to protect.
        ssl_insecure=False,
    )
    if config.mitmproxy_config_dir is not None:
        opts.update(confdir=str(config.mitmproxy_config_dir))
    if config.upstream_ca is not None:
        opts.update(ssl_verify_upstream_trusted_ca=str(config.upstream_ca))

    def on_policy_reload(policy: Policy) -> None:
        """Keep what mitmproxy intercepts in step with what the gate gates."""
        opts.update(allow_hosts=allow_hosts(policy.hosts))
        logger.info(
            f"gating {', '.join(policy.hosts)} for sandboxes {', '.join(policy.sandboxes)}"
        )

    policy = PolicyFile(config.policy_path, on_reload=on_policy_reload)
    policy.load()  # fail here, at startup, rather than on the first call
    tokens = read_tokens(config.tokens_path)
    if tokens:
        logger.info(f"attaching a token to {', '.join(sorted(tokens))}")
    control = ControlServer(config, store, policy, secret)
    # with_dumper off: the control socket is the interface, and a flow dump on
    # the same console would scroll the prompts away.
    #
    # with_termlog off as well, because mitmproxy reports twice over: its
    # TermLog addon prints in its own format *and* the same records go through
    # the stdlib logging that basicConfig below already handles. Leaving only
    # ours keeps one line per event, in one format.
    master = DumpMaster(
        opts,
        loop=asyncio.get_running_loop(),
        with_termlog=False,
        with_dumper=False,
    )
    master.addons.add(GateAddon(policy, control.gate, control.publish, tokens))

    logger.info(f"proxy on {config.proxy_host}:{config.proxy_port}")
    await asyncio.gather(master.run(), control.serve_forever(), watch_policy(policy))


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    if not args.verbose:
        for chatty in ("mitmproxy", "websockets"):
            logging.getLogger(chatty).setLevel(logging.WARNING)
    try:
        config = Config.load(args.config)
    except ConfigError as exc:
        print(f"mcp-gateway: {exc}", file=sys.stderr)
        return 2

    secret = read_secret(config.secret_path)
    logger.info(f"control secret in {config.secret_path}")
    try:
        asyncio.run(run(config, secret))
    except KeyboardInterrupt:
        pass
    return 0
