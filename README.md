# claude-utils

CLI utilities for Claude Code.

## Install

```bash
brew install chasemp/tap/claude-utils
```

## Tools

### claude-sessions

List and resume Claude Code sessions.

```
claude-sessions              # last 3 sessions for current directory
claude-sessions --all        # all sessions, all projects
claude-sessions --path /foo  # sessions for a specific directory
claude-sessions --json       # machine-readable output
claude-sessions --limit 10   # show more (default 3)
```

For interactive resume (pre-fills the command on your prompt line), add this to your `.zshrc`:

```zsh
claude-sessions() {
  local cmd
  cmd=$(command claude-sessions "$@")
  if [[ -n "$cmd" ]]; then
    print -z "$cmd"
  fi
}
```
