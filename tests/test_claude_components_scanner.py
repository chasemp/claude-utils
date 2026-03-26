from __future__ import annotations

import json
import tempfile
from pathlib import Path

from claude_utils.claude_components import (
    ComponentType,
    Scanner,
    Scope,
    inventory_to_json,
    parse_frontmatter,
)


def _setup_home(tmp: Path) -> Path:
    """Create a minimal ~/.claude structure for testing."""
    claude_dir = tmp / ".claude"
    claude_dir.mkdir()
    return claude_dir


def test_parse_frontmatter_extracts_kv_pairs() -> None:
    text = "---\nname: my-agent\ndescription: Does things\nmodel: sonnet\n---\nBody text"
    result = parse_frontmatter(text)
    assert result == {"name": "my-agent", "description": "Does things", "model": "sonnet"}


def test_parse_frontmatter_returns_empty_for_no_frontmatter() -> None:
    assert parse_frontmatter("# Just markdown\nNo frontmatter here") == {}


def test_parse_frontmatter_strips_quotes() -> None:
    text = '---\nname: "my-skill"\ndescription: \'Does stuff\'\n---\n'
    result = parse_frontmatter(text)
    assert result["name"] == "my-skill"
    assert result["description"] == "Does stuff"


def test_scanner_discovers_user_claude_md() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        claude_dir = _setup_home(tmp)
        (claude_dir / "CLAUDE.md").write_text("# Instructions\nDo things.")
        project = tmp / "project"
        project.mkdir()

        scanner = Scanner(user_home=tmp, project_dir=project)
        components = scanner.scan_all()

        claude_mds = [c for c in components if c.component_type == ComponentType.CLAUDE_MD]
        assert len(claude_mds) == 1
        assert claude_mds[0].scope == Scope.USER


def test_scanner_follows_at_imports() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        claude_dir = _setup_home(tmp)
        sub = claude_dir / "sub"
        sub.mkdir()
        (sub / "extra.md").write_text("# Extra instructions")
        (claude_dir / "CLAUDE.md").write_text("# Root\n@sub/extra.md\n")
        project = tmp / "project"
        project.mkdir()

        scanner = Scanner(user_home=tmp, project_dir=project)
        components = scanner.scan_all()

        claude_mds = [c for c in components if c.component_type == ComponentType.CLAUDE_MD]
        assert len(claude_mds) == 2
        names = {c.name for c in claude_mds}
        assert "CLAUDE.md" in names
        assert "extra.md" in names


def test_scanner_discovers_agents() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        claude_dir = _setup_home(tmp)
        agents_dir = claude_dir / "agents"
        agents_dir.mkdir()
        (agents_dir / "tdd-guardian.md").write_text(
            "---\nname: tdd-guardian\ndescription: Enforces TDD\nmodel: sonnet\n---\nBody"
        )
        project = tmp / "project"
        project.mkdir()

        scanner = Scanner(user_home=tmp, project_dir=project)
        components = scanner.scan_all()

        agents = [c for c in components if c.component_type == ComponentType.AGENT]
        assert len(agents) == 1
        assert agents[0].name == "tdd-guardian"
        assert agents[0].metadata["model"] == "sonnet"


def test_scanner_discovers_skill_directories() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        claude_dir = _setup_home(tmp)
        skills_dir = claude_dir / "skills"
        skills_dir.mkdir()
        skill = skills_dir / "code-review"
        skill.mkdir()
        (skill / "SKILL.md").write_text(
            "---\nname: code-review\ndescription: Reviews code\n---\nInstructions"
        )
        (skill / "reference.md").write_text("Extra reference material")
        project = tmp / "project"
        project.mkdir()

        scanner = Scanner(user_home=tmp, project_dir=project)
        components = scanner.scan_all()

        skills = [c for c in components if c.component_type == ComponentType.SKILL]
        assert len(skills) == 1
        assert skills[0].name == "code-review"


def test_scanner_discovers_commands() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        claude_dir = _setup_home(tmp)
        commands_dir = claude_dir / "commands"
        commands_dir.mkdir()
        (commands_dir / "deploy.md").write_text(
            "---\ndescription: Deploy the app\n---\nDeploy steps"
        )
        project = tmp / "project"
        project.mkdir()

        scanner = Scanner(user_home=tmp, project_dir=project)
        components = scanner.scan_all()

        commands = [c for c in components if c.component_type == ComponentType.COMMAND]
        assert len(commands) == 1
        assert commands[0].name == "deploy"


def test_scanner_discovers_hooks_from_settings() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        claude_dir = _setup_home(tmp)

        hook_script = claude_dir / "hooks"
        hook_script.mkdir()
        (hook_script / "guard.sh").write_text("#!/bin/bash\nexit 0")

        settings = {
            "hooks": {
                "PreToolUse": [{
                    "matcher": "Bash",
                    "hooks": [{
                        "type": "command",
                        "command": str(hook_script / "guard.sh"),
                    }],
                }],
            },
        }
        (claude_dir / "settings.json").write_text(json.dumps(settings))
        project = tmp / "project"
        project.mkdir()

        scanner = Scanner(user_home=tmp, project_dir=project)
        components = scanner.scan_all()

        hooks = [c for c in components if c.component_type == ComponentType.HOOK]
        assert len(hooks) == 1
        assert hooks[0].metadata["event"] == "PreToolUse"
        assert hooks[0].metadata["matcher"] == "Bash"
        assert hooks[0].content_hash  # has a hash from the file


def test_scanner_discovers_mcp_servers() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        claude_dir = _setup_home(tmp)
        settings = {
            "mcpServers": {
                "github": {
                    "command": "npx",
                    "args": ["-y", "@mcp/server-github"],
                },
            },
        }
        (claude_dir / "settings.json").write_text(json.dumps(settings))
        project = tmp / "project"
        project.mkdir()

        scanner = Scanner(user_home=tmp, project_dir=project)
        components = scanner.scan_all()

        servers = [c for c in components if c.component_type == ComponentType.MCP_SERVER]
        assert len(servers) == 1
        assert servers[0].name == "github"
        assert servers[0].metadata["command"] == "npx"


def test_scanner_discovers_marketplace_plugins() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        claude_dir = _setup_home(tmp)
        marketplace = claude_dir / "plugins" / "marketplaces" / "official"
        plugin_dir = marketplace / "my-plugin"
        plugin_dir.mkdir(parents=True)
        manifest_dir = plugin_dir / ".claude-plugin"
        manifest_dir.mkdir()
        (manifest_dir / "plugin.json").write_text(json.dumps({
            "name": "my-plugin",
            "version": "1.0.0",
            "description": "Test plugin",
        }))
        (plugin_dir / "README.md").write_text("# My Plugin")
        project = tmp / "project"
        project.mkdir()

        scanner = Scanner(user_home=tmp, project_dir=project)
        components = scanner.scan_all()

        plugins = [c for c in components if c.component_type == ComponentType.PLUGIN]
        assert any(p.name == "my-plugin" for p in plugins)


def test_scanner_discovers_rules() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        claude_dir = _setup_home(tmp)
        project = tmp / "project"
        project.mkdir()
        rules_dir = project / ".claude" / "rules"
        rules_dir.mkdir(parents=True)
        (rules_dir / "code-style.md").write_text("---\npaths:\n  - src/**\n---\nRules")

        scanner = Scanner(user_home=tmp, project_dir=project)
        components = scanner.scan_all()

        rules = [c for c in components if c.component_type == ComponentType.RULE]
        assert len(rules) == 1
        assert rules[0].name == "code-style"


def test_scanner_output_is_deterministic() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        claude_dir = _setup_home(tmp)
        (claude_dir / "CLAUDE.md").write_text("# Root")
        agents_dir = claude_dir / "agents"
        agents_dir.mkdir()
        (agents_dir / "b-agent.md").write_text("---\nname: b-agent\n---\nB")
        (agents_dir / "a-agent.md").write_text("---\nname: a-agent\n---\nA")
        commands_dir = claude_dir / "commands"
        commands_dir.mkdir()
        (commands_dir / "deploy.md").write_text("---\ndescription: Deploy\n---\nD")
        project = tmp / "project"
        project.mkdir()

        results = []
        for _ in range(5):
            scanner = Scanner(user_home=tmp, project_dir=project)
            components = scanner.scan_all()
            results.append(inventory_to_json(components))

        # All 5 runs produce identical output
        assert all(r == results[0] for r in results)


def test_scanner_resolves_symlinks() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        claude_dir = _setup_home(tmp)

        # Create a real agent file elsewhere
        real_dir = tmp / "real-agents"
        real_dir.mkdir()
        (real_dir / "guardian.md").write_text("---\nname: guardian\n---\nBody")

        # Symlink it
        agents_dir = claude_dir / "agents"
        agents_dir.mkdir()
        (agents_dir / "guardian.md").symlink_to(real_dir / "guardian.md")

        project = tmp / "project"
        project.mkdir()

        scanner = Scanner(user_home=tmp, project_dir=project)
        components = scanner.scan_all()

        agents = [c for c in components if c.component_type == ComponentType.AGENT]
        assert len(agents) == 1
        # Path should be resolved to real location
        assert "real-agents" in agents[0].path
        # Symlink origin recorded in metadata
        assert "symlink_from" in agents[0].metadata


def test_scanner_discovers_remote_mcp_from_permissions() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        claude_dir = _setup_home(tmp)
        # settings.json with a local MCP server
        settings = {
            "mcpServers": {"local-server": {"command": "local-mcp"}},
        }
        (claude_dir / "settings.json").write_text(json.dumps(settings))
        # settings.local.json with permission grants referencing remote MCP tools
        local_settings = {
            "permissions": {
                "allow": [
                    "mcp__claude_ai_Slack__slack_send_message",
                    "mcp__claude_ai_Slack__slack_read_channel",
                    "mcp__claude_ai_Notion__notion-search",
                    "mcp__local-server__some_tool",
                ],
            },
        }
        (claude_dir / "settings.local.json").write_text(json.dumps(local_settings))
        project = tmp / "project"
        project.mkdir()

        # Without --remote, only local server found
        scanner = Scanner(user_home=tmp, project_dir=project, include_remote=False)
        components = scanner.scan_all()
        servers = [c for c in components if c.component_type == ComponentType.MCP_SERVER]
        assert len(servers) == 1
        assert servers[0].name == "local-server"

        # With --remote, remote servers also discovered
        scanner = Scanner(user_home=tmp, project_dir=project, include_remote=True)
        components = scanner.scan_all()
        servers = [c for c in components if c.component_type == ComponentType.MCP_SERVER]
        server_names = {s.name for s in servers}
        assert "local-server" in server_names
        assert "claude_ai_Slack" in server_names
        assert "claude_ai_Notion" in server_names
        # local-server should NOT appear as remote (already found as local)
        remote_servers = [s for s in servers if s.scope == Scope.REMOTE]
        assert all(s.name != "local-server" for s in remote_servers)


def test_remote_mcp_includes_tool_list_and_provenance() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        claude_dir = _setup_home(tmp)
        (claude_dir / "settings.json").write_text("{}")
        local_settings = {
            "permissions": {
                "allow": [
                    "mcp__claude_ai_Gmail__gmail_search_messages",
                    "mcp__claude_ai_Gmail__gmail_read_message",
                    "mcp__third_party_ext__do_stuff",
                ],
            },
        }
        (claude_dir / "settings.local.json").write_text(json.dumps(local_settings))
        project = tmp / "project"
        project.mkdir()

        scanner = Scanner(user_home=tmp, project_dir=project, include_remote=True)
        components = scanner.scan_all()
        servers = {
            s.name: s
            for s in components
            if s.component_type == ComponentType.MCP_SERVER
        }

        gmail = servers["claude_ai_Gmail"]
        assert gmail.scope == Scope.REMOTE
        assert gmail.path == "<remote>"
        assert gmail.metadata["provenance"] == "anthropic_hosted"
        assert set(gmail.metadata["tools_observed"]) == {
            "gmail_search_messages", "gmail_read_message"
        }

        third = servers["third_party_ext"]
        assert third.metadata["provenance"] == "unknown"


def test_remote_mcp_output_is_deterministic() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        claude_dir = _setup_home(tmp)
        (claude_dir / "settings.json").write_text("{}")
        local_settings = {
            "permissions": {
                "allow": [
                    "mcp__claude_ai_Slack__slack_send_message",
                    "mcp__claude_ai_Notion__notion-search",
                ],
            },
        }
        (claude_dir / "settings.local.json").write_text(json.dumps(local_settings))
        project = tmp / "project"
        project.mkdir()

        results = []
        for _ in range(5):
            scanner = Scanner(user_home=tmp, project_dir=project, include_remote=True)
            components = scanner.scan_all()
            results.append(inventory_to_json(components))
        assert all(r == results[0] for r in results)
