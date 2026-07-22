# --- sbx personal shell config (appended to ~/.bashrc at build) ---
# Interactive-only bits go here. Env vars that must also reach Claude's
# non-interactive Bash tool live as ENV in the Dockerfile instead.

alias gca='git commit --amend --no-edit'

# Snapshot / restore ~/.claude (writes to the workspace mount so it survives a
# sandbox recreate). See scripts for details.
alias claude-backup='/opt/sbx/claude-backup.sh'
alias claude-restore='/opt/sbx/claude-restore.sh'
