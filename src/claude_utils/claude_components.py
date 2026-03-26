"""Deterministic discovery and inventory of all Claude Code components on macOS.

Combines models, scanner, and CLI into a single module for the claude-components command.
"""
from __future__ import annotations

import argparse
import enum
import hashlib
import json
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path


# ── Models ──────────────────────────────────────────────────────────


class ComponentType(enum.Enum):
    AGENT = "agent"
    CLAUDE_MD = "claude_md"
    COMMAND = "command"
    HOOK = "hook"
    MCP_SERVER = "mcp_server"
    PLUGIN = "plugin"
    RULE = "rule"
    SKILL = "skill"


class Scope(enum.Enum):
    MANAGED = "managed"
    USER = "user"
    PROJECT = "project"
    LOCAL = "local"
    PLUGIN = "plugin"
    REMOTE = "remote"


class Locality(enum.Enum):
    LOCAL = "local"
    REMOTE = "remote"


# Scopes that correspond to locally-defined components (on disk, auditable).
_LOCAL_SCOPES = frozenset({Scope.MANAGED, Scope.USER, Scope.PROJECT, Scope.LOCAL, Scope.PLUGIN})


@dataclass(frozen=True)
class Component:
    component_type: ComponentType
    name: str
    path: str
    content_hash: str
    scope: Scope
    metadata: dict[str, object] = field(default_factory=dict)

    @property
    def locality(self) -> Locality:
        if self.scope in _LOCAL_SCOPES:
            return Locality.LOCAL
        return Locality.REMOTE

    def to_dict(self) -> dict[str, object]:
        return {
            "type": self.component_type.value,
            "name": self.name,
            "path": self.path,
            "hash": self.content_hash,
            "scope": self.scope.value,
            "locality": self.locality.value,
            "metadata": dict(sorted(self.metadata.items())) if self.metadata else {},
        }


def sort_key(component: Component) -> tuple[str, str, str, str]:
    return (
        component.component_type.value,
        component.scope.value,
        component.name,
        component.path,
    )


def hash_file(path: Path) -> str:
    h = hashlib.sha256()
    h.update(path.read_bytes())
    return h.hexdigest()


def hash_directory(path: Path) -> str:
    h = hashlib.sha256()
    entries: list[tuple[str, str]] = []
    for file_path in sorted(path.rglob("*")):
        if file_path.is_file():
            rel = str(file_path.relative_to(path))
            file_hash = hash_file(file_path)
            entries.append((rel, file_hash))
    for rel, file_hash in entries:
        h.update(f"{rel}:{file_hash}\n".encode())
    return h.hexdigest()


def inventory_to_json(components: list[Component]) -> str:
    sorted_components = sorted(components, key=sort_key)
    return json.dumps(
        {"components": [c.to_dict() for c in sorted_components]},
        indent=2,
        sort_keys=True,
    )


# ── Scanner ─────────────────────────────────────────────────────────

# Known Anthropic first-party remote MCP integrations.
KNOWN_REMOTE_MCP_PREFIXES = (
    "claude_ai_",
    "claude-in-chrome",
)

AT_IMPORT_RE = re.compile(r"^@(.+\.md)\s*$", re.MULTILINE)
FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---", re.DOTALL)


def parse_frontmatter(text: str) -> dict[str, str]:
    """Extract YAML-like frontmatter as simple key: value pairs."""
    match = FRONTMATTER_RE.match(text)
    if not match:
        return {}
    result: dict[str, str] = {}
    for line in match.group(1).splitlines():
        if ":" in line and not line.startswith(" ") and not line.startswith("\t"):
            key, _, value = line.partition(":")
            result[key.strip()] = value.strip().strip('"').strip("'")
    return result


def resolve_symlink(path: Path) -> Path:
    """Resolve symlinks to their real target."""
    try:
        return path.resolve()
    except OSError:
        return path


def _git_info(repo_dir: Path) -> dict[str, str]:
    """Extract git remote URL and HEAD commit from a directory, if it's a git repo."""
    result: dict[str, str] = {}
    if not (repo_dir / ".git").exists():
        return result
    try:
        url = subprocess.run(
            ["git", "-C", str(repo_dir), "remote", "get-url", "origin"],
            capture_output=True, text=True, timeout=5,
        )
        if url.returncode == 0 and url.stdout.strip():
            result["git_remote"] = url.stdout.strip()
        commit = subprocess.run(
            ["git", "-C", str(repo_dir), "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=5,
        )
        if commit.returncode == 0 and commit.stdout.strip():
            result["git_commit"] = commit.stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        pass
    return result


def _read_text_safe(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _read_json_safe(path: Path) -> dict[str, object]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))  # type: ignore[return-value]
    except (OSError, json.JSONDecodeError):
        return {}


class Scanner:
    """Discovers all Claude Code components from known filesystem locations."""

    def __init__(
        self,
        *,
        user_home: Path | None = None,
        project_dir: Path | None = None,
        include_remote: bool = False,
        scan_all_projects: bool = False,
    ) -> None:
        self.user_home = user_home or Path.home()
        self.project_dir = project_dir or Path.cwd()
        self.claude_dir = self.user_home / ".claude"
        self.include_remote = include_remote
        self.scan_all_projects = scan_all_projects
        self.components: list[Component] = []
        self._visited_claude_mds: set[str] = set()

    def scan_all(self) -> list[Component]:
        """Run all discovery passes and return deduplicated components."""
        self.components = []
        self._visited_claude_mds = set()

        self._scan_managed_policy()
        self._scan_claude_md_tree()
        self._scan_agents()
        self._scan_skills()
        self._scan_commands()
        self._scan_rules()
        self._scan_settings_hooks()
        self._scan_mcp_servers()
        self._scan_plugins()
        if self.include_remote:
            self._scan_remote_mcp_servers()

        return self._deduplicate()

    # ── CLAUDE.md tree walking ──────────────────────────────────────

    def _scan_managed_policy(self) -> None:
        if sys.platform == "darwin":
            managed = Path("/Library/Application Support/ClaudeCode")
        else:
            managed = Path("/etc/claude-code")
        for name in ("CLAUDE.md", "settings.json"):
            p = managed / name
            if p.is_file():
                self._add_claude_md(p, Scope.MANAGED)

    def _scan_claude_md_tree(self) -> None:
        """Walk CLAUDE.md files starting from user and project roots."""
        user_md = self.claude_dir / "CLAUDE.md"
        if user_md.is_file():
            self._add_claude_md(user_md, Scope.USER)

        project_md = self.project_dir / "CLAUDE.md"
        if project_md.is_file():
            self._add_claude_md(project_md, Scope.PROJECT)

        project_claude_md = self.project_dir / ".claude" / "CLAUDE.md"
        if project_claude_md.is_file():
            self._add_claude_md(project_claude_md, Scope.PROJECT)

    def _add_claude_md(self, path: Path, scope: Scope, depth: int = 0) -> None:
        if depth > 5:
            return
        resolved = resolve_symlink(path)
        key = str(resolved)
        if key in self._visited_claude_mds:
            return
        if not resolved.is_file():
            return
        self._visited_claude_mds.add(key)

        content = _read_text_safe(resolved)

        self.components.append(Component(
            component_type=ComponentType.CLAUDE_MD,
            name=resolved.name,
            path=str(resolved),
            content_hash=hash_file(resolved),
            scope=scope,
            metadata={"symlink_from": str(path)} if path != resolved else {},
        ))

        for match in AT_IMPORT_RE.finditer(content):
            import_ref = match.group(1).strip()
            if import_ref.startswith("~"):
                import_path = self.user_home / import_ref[2:]
            else:
                import_path = path.parent / import_ref
            self._add_claude_md(import_path, scope, depth + 1)

    # ── Agents ──────────────────────────────────────────────────────

    def _scan_agents(self) -> None:
        agent_dirs = [
            (self.claude_dir / "agents", Scope.USER),
            (self.project_dir / ".claude" / "agents", Scope.PROJECT),
        ]
        for agent_dir, scope in agent_dirs:
            self._scan_md_components(agent_dir, ComponentType.AGENT, scope)

    def _scan_md_components(
        self, directory: Path, comp_type: ComponentType, scope: Scope
    ) -> None:
        if not directory.is_dir():
            return
        for md_file in sorted(directory.iterdir()):
            resolved = resolve_symlink(md_file)
            if not resolved.is_file() or not resolved.name.endswith(".md"):
                continue
            content = _read_text_safe(resolved)
            fm = parse_frontmatter(content)
            name = fm.get("name", resolved.stem)
            metadata: dict[str, object] = {}
            if md_file != resolved:
                metadata["symlink_from"] = str(md_file)
            for key in ("description", "model", "maxTurns", "memory", "tools"):
                if key in fm:
                    metadata[key] = fm[key]
            self.components.append(Component(
                component_type=comp_type,
                name=name,
                path=str(resolved),
                content_hash=hash_file(resolved),
                scope=scope,
                metadata=metadata,
            ))

    # ── Skills ──────────────────────────────────────────────────────

    def _scan_skills(self) -> None:
        skill_dirs = [
            (self.claude_dir / "skills", Scope.USER),
            (self.project_dir / ".claude" / "skills", Scope.PROJECT),
        ]
        for skill_dir, scope in skill_dirs:
            self._scan_skill_directory(skill_dir, scope)

    def _scan_skill_directory(self, directory: Path, scope: Scope) -> None:
        if not directory.is_dir():
            return
        for entry in sorted(directory.iterdir()):
            resolved = resolve_symlink(entry)
            if resolved.is_dir():
                skill_md = resolved / "SKILL.md"
                if skill_md.is_file():
                    content = _read_text_safe(skill_md)
                    fm = parse_frontmatter(content)
                    name = fm.get("name", resolved.name)
                    metadata: dict[str, object] = {}
                    if entry != resolved:
                        metadata["symlink_from"] = str(entry)
                    skill_md_resolved = resolve_symlink(skill_md)
                    if skill_md_resolved != skill_md:
                        metadata["skill_md_target"] = str(skill_md_resolved)
                    for key in ("description", "user-invocable", "allowed-tools"):
                        if key in fm:
                            metadata[key] = fm[key]
                    git = _git_info(resolved)
                    if not git:
                        git = _git_info(resolved.parent)
                    if not git and skill_md_resolved != skill_md:
                        git = _git_info(skill_md_resolved.parent)
                    metadata.update(git)
                    self.components.append(Component(
                        component_type=ComponentType.SKILL,
                        name=name,
                        path=str(resolved),
                        content_hash=hash_directory(resolved),
                        scope=scope,
                        metadata=metadata,
                    ))
            elif resolved.is_file() and resolved.name.endswith(".md"):
                content = _read_text_safe(resolved)
                fm = parse_frontmatter(content)
                if fm.get("name") or fm.get("description"):
                    name = fm.get("name", resolved.stem)
                    metadata = {}
                    if entry != resolved:
                        metadata["symlink_from"] = str(entry)
                    for key in ("description", "user-invocable"):
                        if key in fm:
                            metadata[key] = fm[key]
                    self.components.append(Component(
                        component_type=ComponentType.SKILL,
                        name=name,
                        path=str(resolved),
                        content_hash=hash_file(resolved),
                        scope=scope,
                        metadata=metadata,
                    ))

    # ── Commands ────────────────────────────────────────────────────

    def _scan_commands(self) -> None:
        command_dirs = [
            (self.claude_dir / "commands", Scope.USER),
            (self.project_dir / ".claude" / "commands", Scope.PROJECT),
        ]
        for cmd_dir, scope in command_dirs:
            self._scan_md_components(cmd_dir, ComponentType.COMMAND, scope)

    # ── Rules ───────────────────────────────────────────────────────

    def _scan_rules(self) -> None:
        rules_dir = self.project_dir / ".claude" / "rules"
        if not rules_dir.is_dir():
            return
        for md_file in sorted(rules_dir.rglob("*.md")):
            resolved = resolve_symlink(md_file)
            if not resolved.is_file():
                continue
            content = _read_text_safe(resolved)
            fm = parse_frontmatter(content)
            metadata: dict[str, object] = {}
            if "paths" in fm:
                metadata["paths"] = fm["paths"]
            self.components.append(Component(
                component_type=ComponentType.RULE,
                name=resolved.stem,
                path=str(resolved),
                content_hash=hash_file(resolved),
                scope=Scope.PROJECT,
                metadata=metadata,
            ))

    # ── Hooks (from settings.json) ──────────────────────────────────

    def _scan_settings_hooks(self) -> None:
        settings_files = [
            (self.claude_dir / "settings.json", Scope.USER),
            (self.project_dir / ".claude" / "settings.json", Scope.PROJECT),
            (self.project_dir / ".claude" / "settings.local.json", Scope.LOCAL),
        ]
        for settings_path, scope in settings_files:
            data = _read_json_safe(settings_path)
            hooks = data.get("hooks", {})
            if not isinstance(hooks, dict):
                continue
            for event_name, hook_list in sorted(hooks.items()):
                if not isinstance(hook_list, list):
                    continue
                for idx, entry in enumerate(hook_list):
                    if not isinstance(entry, dict):
                        continue
                    matcher = entry.get("matcher", "*")
                    inner_hooks = entry.get("hooks", [])
                    if not isinstance(inner_hooks, list):
                        continue
                    for hidx, hook_def in enumerate(inner_hooks):
                        if not isinstance(hook_def, dict):
                            continue
                        hook_type = hook_def.get("type", "unknown")
                        command = hook_def.get("command", "")
                        hook_name = f"{event_name}:{matcher}:{hidx}"

                        metadata: dict[str, object] = {
                            "event": event_name,
                            "matcher": str(matcher),
                            "hook_type": hook_type,
                        }

                        content_hash = ""
                        hook_path = str(settings_path)

                        if hook_type == "command" and command:
                            cmd_path = Path(command).expanduser()
                            if not cmd_path.is_absolute():
                                cmd_path = settings_path.parent / cmd_path
                            resolved_cmd = resolve_symlink(cmd_path)
                            if resolved_cmd.is_file():
                                content_hash = hash_file(resolved_cmd)
                                hook_path = str(resolved_cmd)
                                if cmd_path != resolved_cmd:
                                    metadata["symlink_from"] = str(cmd_path)
                            else:
                                metadata["command"] = command
                                content_hash = ""
                        elif hook_type == "prompt":
                            prompt = hook_def.get("prompt", "")
                            metadata["prompt_preview"] = str(prompt)[:100]
                            content_hash = hashlib.sha256(
                                str(prompt).encode()
                            ).hexdigest()

                        self.components.append(Component(
                            component_type=ComponentType.HOOK,
                            name=hook_name,
                            path=hook_path,
                            content_hash=content_hash,
                            scope=scope,
                            metadata=metadata,
                        ))

    # ── MCP Servers ─────────────────────────────────────────────────

    def _scan_mcp_servers(self) -> None:
        sources = [
            (self.claude_dir / "settings.json", Scope.USER),
            (self.project_dir / ".claude" / "settings.json", Scope.PROJECT),
            (self.project_dir / ".claude" / "settings.local.json", Scope.LOCAL),
            (self.project_dir / ".mcp.json", Scope.PROJECT),
            (self.claude_dir / ".mcp.json", Scope.USER),
        ]
        seen_servers: set[str] = set()
        for source_path, scope in sources:
            data = _read_json_safe(source_path)
            servers = data.get("mcpServers", {})
            if not isinstance(servers, dict):
                continue
            for server_name, config in sorted(servers.items()):
                key = f"{scope.value}:{server_name}"
                if key in seen_servers:
                    continue
                seen_servers.add(key)
                if not isinstance(config, dict):
                    continue
                metadata: dict[str, object] = {
                    "source_file": str(source_path),
                }
                for field_name in ("command", "type", "url"):
                    if field_name in config:
                        metadata[field_name] = config[field_name]
                if "args" in config and isinstance(config["args"], list):
                    metadata["args"] = " ".join(str(a) for a in config["args"])
                config_str = json.dumps(config, sort_keys=True)
                content_hash = hashlib.sha256(config_str.encode()).hexdigest()
                self.components.append(Component(
                    component_type=ComponentType.MCP_SERVER,
                    name=server_name,
                    path=str(source_path),
                    content_hash=content_hash,
                    scope=scope,
                    metadata=metadata,
                ))

    # ── Remote MCP Servers ───────────────────────────────────────────

    def _scan_remote_mcp_servers(self) -> None:
        MCP_TOOL_RE = re.compile(r"mcp__([a-zA-Z0-9_-]+)__(\w+)")
        remote_servers: dict[str, set[str]] = {}
        source_files: dict[str, set[str]] = {}

        settings_files = [
            self.claude_dir / "settings.json",
            self.claude_dir / "settings.local.json",
            self.project_dir / ".claude" / "settings.json",
            self.project_dir / ".claude" / "settings.local.json",
        ]

        for settings_path in settings_files:
            data = _read_json_safe(settings_path)
            self._extract_mcp_refs_from_settings(
                data, settings_path, MCP_TOOL_RE, remote_servers, source_files
            )

        projects_dir = self.claude_dir / "projects"
        if projects_dir.is_dir():
            for project in sorted(projects_dir.iterdir()):
                if not project.is_dir():
                    continue
                for settings_name in ("settings.json", "settings.local.json"):
                    sp = project / settings_name
                    if sp.is_file():
                        data = _read_json_safe(sp)
                        self._extract_mcp_refs_from_settings(
                            data, sp, MCP_TOOL_RE, remote_servers, source_files
                        )

        if self.scan_all_projects:
            self._walk_project_settings(
                MCP_TOOL_RE, remote_servers, source_files
            )

        local_server_names = {
            c.name
            for c in self.components
            if c.component_type == ComponentType.MCP_SERVER
        }

        for server_name in sorted(remote_servers.keys()):
            if server_name in local_server_names:
                continue

            tools = sorted(remote_servers[server_name])
            sources = sorted(source_files.get(server_name, set()))

            is_anthropic_hosted = any(
                server_name.startswith(p) for p in KNOWN_REMOTE_MCP_PREFIXES
            )
            provenance = "anthropic_hosted" if is_anthropic_hosted else "unknown"

            hash_input = f"{server_name}:{','.join(tools)}"
            content_hash = hashlib.sha256(hash_input.encode()).hexdigest()

            metadata: dict[str, object] = {
                "provenance": provenance,
                "tools_observed": tools,
                "tool_count": len(tools),
                "discovered_from": sources,
            }

            self.components.append(Component(
                component_type=ComponentType.MCP_SERVER,
                name=server_name,
                path="<remote>",
                content_hash=content_hash,
                scope=Scope.REMOTE,
                metadata=metadata,
            ))

    def _extract_mcp_refs_from_settings(
        self,
        data: dict[str, object],
        source_path: Path,
        pattern: re.Pattern[str],
        servers: dict[str, set[str]],
        source_files: dict[str, set[str]],
    ) -> None:
        text = json.dumps(data)
        for match in pattern.finditer(text):
            server_name = match.group(1)
            tool_name = match.group(2)
            servers.setdefault(server_name, set()).add(tool_name)
            source_files.setdefault(server_name, set()).add(str(source_path))

    def _walk_project_settings(
        self,
        pattern: re.Pattern[str],
        servers: dict[str, set[str]],
        source_files: dict[str, set[str]],
    ) -> None:
        code_roots = [
            self.user_home / "git",
            self.user_home / "src",
            self.user_home / "code",
            self.user_home / "projects",
            self.user_home / "repos",
            self.user_home / "workspace",
            self.user_home / "dev",
            self.user_home / "work",
        ]
        seen: set[str] = set()
        for root in code_roots:
            if not root.is_dir():
                continue
            try:
                for settings_path in sorted(root.rglob(".claude/settings*.json")):
                    if not settings_path.is_file():
                        continue
                    key = str(settings_path.resolve())
                    if key in seen:
                        continue
                    seen.add(key)
                    if settings_path.parent == self.claude_dir:
                        continue
                    data = _read_json_safe(settings_path)
                    self._extract_mcp_refs_from_settings(
                        data, settings_path, pattern, servers, source_files
                    )
            except (OSError, TimeoutError):
                continue

    # ── Plugins ─────────────────────────────────────────────────────

    def _scan_plugins(self) -> None:
        plugins_dir = self.claude_dir / "plugins"
        if not plugins_dir.is_dir():
            return

        registry_path = plugins_dir / "installed_plugins.json"
        if registry_path.is_file():
            self._scan_plugin_registry(registry_path)

        marketplaces_dir = plugins_dir / "marketplaces"
        if marketplaces_dir.is_dir():
            for marketplace in sorted(marketplaces_dir.iterdir()):
                if marketplace.is_dir():
                    self._scan_marketplace(marketplace)

        cache_dir = plugins_dir / "cache"
        if cache_dir.is_dir():
            self._scan_plugin_cache(cache_dir)

        self._scan_enabled_plugins()

    def _scan_plugin_registry(self, registry_path: Path) -> None:
        data = _read_json_safe(registry_path)
        if not isinstance(data, (dict, list)):
            return
        plugins = data if isinstance(data, list) else data.get("plugins", [])
        if not isinstance(plugins, list):
            if isinstance(data, dict):
                for name, info in sorted(data.items()):
                    if name == "plugins":
                        continue
                    if isinstance(info, dict):
                        self.components.append(Component(
                            component_type=ComponentType.PLUGIN,
                            name=str(name),
                            path=str(registry_path),
                            content_hash=hash_file(registry_path),
                            scope=Scope.USER,
                            metadata={
                                k: v for k, v in sorted(info.items())
                                if isinstance(v, (str, bool, int, float))
                            },
                        ))

    def _scan_marketplace(self, marketplace_dir: Path) -> None:
        marketplace_git = _git_info(marketplace_dir)

        settings = _read_json_safe(self.claude_dir / "settings.json")
        known_marketplaces = settings.get("extraKnownMarketplaces", {})
        if isinstance(known_marketplaces, dict):
            mp_config = known_marketplaces.get(marketplace_dir.name, {})
            if isinstance(mp_config, dict):
                source = mp_config.get("source", {})
                if isinstance(source, dict) and "url" in source:
                    marketplace_git.setdefault("git_remote", source["url"])

        plugin_containers = ["plugins", "skills"]
        scanned_any = False

        for container_name in plugin_containers:
            container = marketplace_dir / container_name
            if not container.is_dir():
                continue
            for plugin_dir in sorted(container.iterdir()):
                if not plugin_dir.is_dir():
                    continue
                if plugin_dir.name.startswith("."):
                    continue
                self._add_marketplace_plugin(
                    plugin_dir, marketplace_dir.name, container_name,
                    marketplace_git,
                )
                scanned_any = True

        if not scanned_any:
            for entry in sorted(marketplace_dir.iterdir()):
                if not entry.is_dir() or entry.name.startswith("."):
                    continue
                if (
                    (entry / ".claude-plugin" / "plugin.json").is_file()
                    or (entry / "SKILL.md").is_file()
                ):
                    self._add_marketplace_plugin(
                        entry, marketplace_dir.name, "root",
                        marketplace_git,
                    )

    def _add_marketplace_plugin(
        self,
        plugin_dir: Path,
        marketplace_name: str,
        container: str,
        marketplace_git: dict[str, str] | None = None,
    ) -> None:
        metadata: dict[str, object] = {
            "marketplace": marketplace_name,
        }

        if marketplace_git:
            for k, v in marketplace_git.items():
                metadata[k] = v

        manifest_path = plugin_dir / ".claude-plugin" / "plugin.json"
        skill_md = plugin_dir / "SKILL.md"

        if manifest_path.is_file():
            manifest = _read_json_safe(manifest_path)
            for key in ("name", "version", "description", "author",
                        "repository", "homepage"):
                if key in manifest:
                    val = manifest[key]
                    metadata[key] = str(val) if not isinstance(val, str) else val

        if skill_md.is_file():
            fm = parse_frontmatter(_read_text_safe(skill_md))
            for key in ("name", "description"):
                if key in fm:
                    metadata[key] = fm[key]

        name = str(metadata.get("name", plugin_dir.name))
        self.components.append(Component(
            component_type=ComponentType.PLUGIN,
            name=name,
            path=str(plugin_dir),
            content_hash=hash_directory(plugin_dir),
            scope=Scope.PLUGIN,
            metadata=metadata,
        ))

    def _scan_plugin_cache(self, cache_dir: Path) -> None:
        for source_dir in sorted(cache_dir.iterdir()):
            if not source_dir.is_dir():
                continue
            for version_dir in sorted(source_dir.iterdir()):
                if not version_dir.is_dir():
                    continue
                metadata: dict[str, object] = {
                    "source": source_dir.name,
                    "version": version_dir.name,
                }
                manifest_path = version_dir / ".claude-plugin" / "plugin.json"
                if manifest_path.is_file():
                    manifest = _read_json_safe(manifest_path)
                    for key in ("name", "version", "description"):
                        if key in manifest:
                            metadata[key] = str(manifest[key])
                skills_dir = version_dir / "skills"
                if skills_dir.is_dir():
                    for skill_entry in sorted(skills_dir.iterdir()):
                        if skill_entry.is_dir():
                            skill_md = skill_entry / "SKILL.md"
                            if skill_md.is_file():
                                fm = parse_frontmatter(
                                    _read_text_safe(skill_md)
                                )
                                if fm.get("name") or fm.get("description"):
                                    metadata.setdefault("skills", [])
                                    if isinstance(metadata["skills"], list):
                                        metadata["skills"].append(  # type: ignore[union-attr]
                                            fm.get("name", skill_entry.name)
                                        )

                name = str(metadata.get("name", f"{source_dir.name}/{version_dir.name}"))
                self.components.append(Component(
                    component_type=ComponentType.PLUGIN,
                    name=name,
                    path=str(version_dir),
                    content_hash=hash_directory(version_dir),
                    scope=Scope.PLUGIN,
                    metadata=metadata,
                ))

    def _scan_enabled_plugins(self) -> None:
        for settings_path, scope in [
            (self.claude_dir / "settings.json", Scope.USER),
            (self.project_dir / ".claude" / "settings.json", Scope.PROJECT),
        ]:
            data = _read_json_safe(settings_path)
            enabled = data.get("enabledPlugins", {})
            if not isinstance(enabled, dict):
                continue
            for plugin_ref, is_enabled in sorted(enabled.items()):
                if not is_enabled:
                    continue
                existing = {
                    c.name for c in self.components
                    if c.component_type == ComponentType.PLUGIN
                }
                if plugin_ref not in existing:
                    self.components.append(Component(
                        component_type=ComponentType.PLUGIN,
                        name=plugin_ref,
                        path=str(settings_path),
                        content_hash=hashlib.sha256(
                            plugin_ref.encode()
                        ).hexdigest(),
                        scope=scope,
                        metadata={"enabled": True, "source_file": str(settings_path)},
                    ))

    # ── Deduplication ───────────────────────────────────────────────

    def _deduplicate(self) -> list[Component]:
        seen: dict[str, Component] = {}
        for c in self.components:
            key = f"{c.component_type.value}:{c.scope.value}:{c.name}:{c.path}"
            if key not in seen:
                seen[key] = c
        return list(seen.values())


# ── CLI ─────────────────────────────────────────────────────────────


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Discover and inventory all Claude Code components on this machine.",
    )
    parser.add_argument(
        "--project-dir",
        type=Path,
        default=Path.cwd(),
        help="Project directory to scan for project-scoped components (default: cwd)",
    )
    parser.add_argument(
        "--home",
        type=Path,
        default=Path.home(),
        help="User home directory (default: ~)",
    )
    parser.add_argument(
        "--type",
        dest="filter_type",
        choices=[
            "agent", "claude_md", "command", "hook",
            "mcp_server", "plugin", "rule", "skill",
        ],
        action="append",
        help="Filter to specific component types (can be repeated)",
    )
    parser.add_argument(
        "--remote",
        action="store_true",
        help="Include remote/injected MCP servers inferred from permission grants",
    )
    parser.add_argument(
        "--scan-all-projects",
        action="store_true",
        help="Walk all .claude/settings*.json under $HOME for cross-project MCP discovery (use with --remote)",
    )
    parser.add_argument(
        "--compact",
        action="store_true",
        help="Compact JSON output (no indentation)",
    )
    args = parser.parse_args()

    scanner = Scanner(
        user_home=args.home,
        project_dir=args.project_dir,
        include_remote=args.remote,
        scan_all_projects=args.scan_all_projects,
    )
    components = scanner.scan_all()

    if args.filter_type:
        components = [
            c for c in components
            if c.component_type.value in args.filter_type
        ]

    output = inventory_to_json(components)
    if args.compact:
        output = json.dumps(json.loads(output), sort_keys=True, separators=(",", ":"))

    sys.stdout.write(output + "\n")


if __name__ == "__main__":
    main()
