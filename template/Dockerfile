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
# PATH gains /opt/sbx/bin so the commands in bin/ are callable everywhere —
# interactive shells, Claude's Bash tool, `sbx exec`, scripts. It also has to
# come first: the claude and uv wrappers there only work by shadowing the real
# binaries.
ENV PYTHON_BASIC_REPL=1 \
    DISABLE_AUTOUPDATER=1 \
    PATH=/opt/sbx/bin:$PATH

USER root

RUN apt-get update \
 && apt-get install -y --no-install-recommends nano \
 && rm -rf /var/lib/apt/lists/*

USER agent
RUN claude update
RUN curl -LsSf https://astral.sh/uv/install.sh | sh

# Config comes last on purpose: editing a script must not invalidate the layers
# above (`claude update` alone takes minutes).
#
# Commands go on PATH (see ENV above); assets stay plain data. --chmod covers
# both, so no chmod step has to be kept in sync by hand — and it normalises the
# mode, which otherwise comes from the host fs (0777 on a Windows mount).
# init-config.py runs under python3 and statusline-command.sh is installed with
# its own mode by init-config.py, so neither needs to be executable here.
USER root
COPY --chmod=0755 bin/ /opt/sbx/bin/
COPY --chmod=0644 scripts/ /opt/sbx/

USER agent
RUN cat /opt/sbx/bashrc-extra.sh >> /home/agent/.bashrc
