FROM docker/sandbox-templates:claude-code-docker

# Env vars must reach both interactive shells and Claude's non-interactive Bash
# tool, so they go here (not ~/.bashrc).
#
# DISABLE_AUTOUPDATER=1 turns off Claude's background auto-updater — it can't
# reach downloads.claude.ai through the sandbox's MITM proxy (socket hang up) and
# just spams errors. Updates happen at sandbox start via /opt/sbx/claude-update.sh
# instead (it strips the proxy env). Explicit `claude update` still works with
# this set.
# UV_LINK_MODE=symlink because venvs here are bind mounts (see venv-bind.sh) and
# the kernel refuses hardlinks across a mount boundary — EXDEV even when both
# sides sit on the same filesystem. uv's default would try to hardlink from its
# cache, fail, and fall back to copying every file into every repo's venv. Cross-
# mount symlinks are fine, so the cache stays shared and nothing is copied. The
# trade-off: a venv now depends on its cache entries, so `uv cache clean` breaks
# existing venvs (re-run `uv sync`). Set copy instead if that bites.
ENV PYTHON_BASIC_REPL=1 \
    DISABLE_AUTOUPDATER=1 \
    UV_LINK_MODE=symlink

USER root

RUN apt-get update \
 && apt-get install -y --no-install-recommends nano \
 && rm -rf /var/lib/apt/lists/*

COPY scripts/ /opt/sbx/
RUN chmod 0755 /opt/sbx/init-config.py /opt/sbx/statusline-command.sh \
               /opt/sbx/claude-backup.sh /opt/sbx/claude-restore.sh \
               /opt/sbx/claude-update.sh /opt/sbx/venv-bind.sh

USER agent
RUN claude update
RUN curl -LsSf https://astral.sh/uv/install.sh | sh
RUN cat /opt/sbx/bashrc-extra.sh >> /home/agent/.bashrc
