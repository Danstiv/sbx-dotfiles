# --- sbx personal shell config (appended to ~/.bashrc at build) ---
# Interactive-only bits go here. Anything that must also work outside an
# interactive shell belongs elsewhere: env vars as ENV in the Dockerfile,
# commands as executables in bin/ (they land on PATH via /opt/sbx/bin).

alias gca='git commit --amend --no-edit'
