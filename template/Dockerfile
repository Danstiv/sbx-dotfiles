FROM docker/sandbox-templates:claude-code-docker

# Env vars must reach both interactive shells and Claude's non-interactive Bash
# tool, so they go here (not ~/.bashrc).
#
# The venv goes on the VM's native fs, NOT the workspace. The workspace is a
# virtiofs passthrough of a Windows folder that can't create the interpreter
# symlink uv needs (fails with EPERM "Operation not permitted"; uv bug astral-sh/uv#2103),
# and it's slow/incoherent under churn. An absolute path also keeps the venv off
# the host's own .venv. Caveat: uv uses one absolute UV_PROJECT_ENVIRONMENT for
# ALL projects — fine in the one-project-per-sandbox model.
ENV PYTHON_BASIC_REPL=1 \
    UV_PROJECT_ENVIRONMENT=/home/agent/.venv

USER root

RUN apt-get update \
 && apt-get install -y --no-install-recommends nano \
 && rm -rf /var/lib/apt/lists/*

COPY scripts/ /opt/sbx/
RUN chmod 0755 /opt/sbx/init-config.py /opt/sbx/statusline-command.sh \
               /opt/sbx/claude-backup.sh /opt/sbx/claude-restore.sh

USER agent
RUN claude update
RUN curl -LsSf https://astral.sh/uv/install.sh | sh
RUN cat /opt/sbx/bashrc-extra.sh >> /home/agent/.bashrc
