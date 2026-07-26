FROM docker/sandbox-templates:claude-code-docker

# Env vars must reach both interactive shells and Claude's non-interactive Bash
# tool, so they go here (not ~/.bashrc).
#
# DISABLE_AUTOUPDATER=1 turns off Claude's background auto-updater — it can't
# reach downloads.claude.ai through the sandbox's MITM proxy (socket hang up) and
# just spams errors. Updates happen at sandbox start via the claude-update
# command instead (it strips the proxy env). Explicit `claude update` still
# works with this set.
#
# UV_LINK_MODE=symlink because venvs here are bind mounts (see the venv-bind
# command) and the kernel refuses hardlinks across a mount boundary — EXDEV even
# when both sides sit on the same filesystem. uv's default would try to hardlink
# from its cache, fail, and fall back to copying every file into every repo's
# venv. Cross-mount symlinks are fine, so the cache stays shared and nothing is
# copied. The trade-off: a venv then depends on its cache entries, so
# `uv cache clean` breaks existing venvs (re-run `uv sync`). Use copy if that
# bites.
#
# PATH gains /opt/sbx/bin so the commands in bin/ are callable everywhere —
# interactive shells, Claude's Bash tool, `sbx exec`, scripts.
ENV PYTHON_BASIC_REPL=1 \
    DISABLE_AUTOUPDATER=1 \
    UV_LINK_MODE=symlink \
    PATH=/opt/sbx/bin:$PATH

USER root

RUN apt-get update \
 && apt-get install -y --no-install-recommends nano \
 && rm -rf /var/lib/apt/lists/*

# Commands go on PATH (see ENV above); assets stay plain data. Nothing needs an
# explicit chmod: --chmod covers everything in bin/, init-config.py runs under
# python3, and statusline-command.sh is installed with its own mode by
# init-config.py.
COPY --chmod=0755 bin/ /opt/sbx/bin/
COPY scripts/ /opt/sbx/

USER agent
RUN claude update
RUN curl -LsSf https://astral.sh/uv/install.sh | sh
RUN cat /opt/sbx/bashrc-extra.sh >> /home/agent/.bashrc
