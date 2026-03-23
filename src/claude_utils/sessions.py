"""List Claude Code sessions from disk with optional interactive resume.

Usage:
  claude-sessions              # last 3 sessions for current directory
  claude-sessions --all        # all projects
  claude-sessions --path /foo  # sessions for a specific directory
  claude-sessions --json       # machine-readable output (non-interactive)
  claude-sessions --limit 10   # show more than the default 3
"""

import argparse
import glob
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path


def encode_project_path(path: str) -> str:
    """Encode a directory path to Claude's project directory name format."""
    return path.replace("/", "-")


def decode_project_path(encoded: str) -> str:
    """Best-effort decode of project dir name back to a path."""
    if encoded == "-":
        return "/"
    parts = encoded.split("-")
    return "/" + "/".join(p for p in parts if p)


def extract_session_meta(jsonl_path: str) -> dict | None:
    """Extract metadata from a session JSONL file."""
    session_id = Path(jsonl_path).stem
    first_user_ts = None
    last_ts = None
    first_user_msg = None
    session_name = None
    git_branch = None
    cwd = None
    msg_count = 0
    user_msg_count = 0

    try:
        with open(jsonl_path) as f:
            for line in f:
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue

                msg_type = obj.get("type", "")
                ts = obj.get("timestamp")

                if ts:
                    last_ts = ts

                if msg_type == "user":
                    user_msg_count += 1
                    if first_user_ts is None:
                        first_user_ts = ts
                        git_branch = obj.get("gitBranch")
                        cwd = obj.get("cwd")
                        msg = obj.get("message", {})
                        content = msg.get("content", "")
                        if isinstance(content, str):
                            first_user_msg = content
                        elif isinstance(content, list):
                            for c in content:
                                if isinstance(c, dict) and c.get("type") == "text":
                                    first_user_msg = c["text"]
                                    break

                if msg_type in ("user", "assistant"):
                    msg_count += 1

                if msg_type == "session_name":
                    session_name = obj.get("name")

        if first_user_ts is None:
            return None

        return {
            "session_id": session_id,
            "name": session_name,
            "first_msg": first_user_msg[:120] if first_user_msg else None,
            "started": first_user_ts,
            "last_active": last_ts or first_user_ts,
            "messages": msg_count,
            "user_messages": user_msg_count,
            "git_branch": git_branch,
            "cwd": cwd,
        }
    except (OSError, PermissionError):
        return None


def format_time(iso_str: str) -> str:
    """Format ISO timestamp to a human-friendly relative or absolute string."""
    try:
        dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
        now = datetime.now(timezone.utc)
        delta = now - dt
        if delta.days == 0:
            hours = delta.seconds // 3600
            if hours == 0:
                mins = delta.seconds // 60
                return f"{mins}m ago" if mins > 0 else "just now"
            return f"{hours}h ago"
        if delta.days == 1:
            return "yesterday"
        if delta.days < 7:
            return f"{delta.days}d ago"
        return dt.strftime("%Y-%m-%d")
    except (ValueError, TypeError):
        return iso_str or "unknown"


def is_interactive() -> bool:
    """Check if stdin and stderr are both attached to a terminal.

    We check stderr (not stdout) because the shell wrapper captures stdout
    via command substitution, but stderr still goes to the terminal.
    """
    return sys.stdin.isatty() and sys.stderr.isatty()


def eprint(*args, **kwargs) -> None:
    """Print to stderr (display output)."""
    print(*args, file=sys.stderr, **kwargs)


def print_session_list(sessions: list[dict], show_project: bool) -> None:
    """Print numbered session list to stderr."""
    for i, s in enumerate(sessions, 1):
        preview = s["name"] or (
            s["first_msg"][:75] + "..." if s["first_msg"] and len(s["first_msg"]) > 75
            else s["first_msg"] or ""
        )
        branch = s["git_branch"] or ""
        last = format_time(s["last_active"])
        msgs = s["messages"]

        num = f"\033[33m{i})\033[0m"
        eprint(f"{num} {s['session_id']}  {last}  |  {msgs} msgs  |  branch: {branch}")
        if show_project:
            eprint(f"   project:  {s['project']}")
        if preview:
            eprint(f"   \033[1m{preview}\033[0m")
        eprint()


def prompt_selection(sessions: list[dict]) -> None:
    """Prompt user to pick a session; emit bare command to stdout."""
    try:
        eprint(f"Select [1-{len(sessions)}] or q: ", end="")
        choice = input().strip()
    except (EOFError, KeyboardInterrupt):
        eprint()
        return

    if choice.lower() in ("q", ""):
        return

    try:
        idx = int(choice) - 1
    except ValueError:
        eprint(f"Invalid selection: {choice}")
        return

    if idx < 0 or idx >= len(sessions):
        eprint(f"Out of range: pick 1-{len(sessions)}")
        return

    sid = sessions[idx]["session_id"]
    # Bare command to stdout -- shell wrapper can capture this
    print(f"claude --resume {sid}")


def main():
    epilog = """\
examples:
  claude-sessions              # last 3 sessions for current directory
  claude-sessions --all        # all sessions, all projects
  claude-sessions --path /foo  # sessions for a specific directory
  claude-sessions --json       # machine-readable output
  claude-sessions --limit 10   # show more (default 3)
"""
    parser = argparse.ArgumentParser(
        description="List Claude Code sessions",
        epilog=epilog,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--all", action="store_true", help="Show all projects")
    parser.add_argument("--path", type=str, help="Filter to a specific directory")
    parser.add_argument("--json", action="store_true", help="JSON output")
    parser.add_argument("--limit", type=int, default=3, help="Max sessions to show (default: 3)")
    args = parser.parse_args()

    base = os.path.expanduser("~/.claude/projects")
    if not os.path.isdir(base):
        print("No sessions found.", file=sys.stderr)
        sys.exit(1)

    target_path = args.path or os.getcwd()
    target_encoded = encode_project_path(target_path)

    sessions = []

    for project_dir in os.listdir(base):
        project_full = os.path.join(base, project_dir)
        if not os.path.isdir(project_full):
            continue

        if not args.all and project_dir != target_encoded:
            continue

        for jsonl in glob.glob(os.path.join(project_full, "*.jsonl")):
            meta = extract_session_meta(jsonl)
            if meta:
                meta["project"] = decode_project_path(project_dir)
                sessions.append(meta)

    sessions.sort(key=lambda s: s.get("last_active", ""), reverse=True)
    sessions = sessions[: args.limit]

    if not sessions:
        scope = "any project" if args.all else target_path
        print(f"No sessions found for {scope}.", file=sys.stderr)
        sys.exit(0)

    if args.json:
        print(json.dumps(sessions, indent=2))
        return

    print_session_list(sessions, show_project=args.all)

    if is_interactive():
        prompt_selection(sessions)
    else:
        eprint("Resume with: claude --resume <session_id>")
