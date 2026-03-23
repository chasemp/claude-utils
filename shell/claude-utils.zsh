# claude-utils shell integration
# Source this file from your .zshrc:
#   source "$(brew --prefix)/share/claude-utils/claude-utils.zsh"

# claude-sessions: pre-fills the resume command on the prompt line
claude-sessions() {
  local cmd
  cmd=$(command claude-sessions "$@")
  if [[ -n "$cmd" ]]; then
    print -z "$cmd"
  fi
}
