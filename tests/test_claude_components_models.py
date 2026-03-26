from __future__ import annotations

import json
import tempfile
from pathlib import Path

from claude_utils.claude_components import (
    Component,
    ComponentType,
    Locality,
    Scope,
    hash_directory,
    hash_file,
    inventory_to_json,
    sort_key,
)


def test_component_to_dict_includes_all_fields() -> None:
    component = Component(
        component_type=ComponentType.SKILL,
        name="testing-anti-patterns",
        path="/home/user/.claude/skills/testing-anti-patterns/SKILL.md",
        content_hash="abc123",
        scope=Scope.USER,
        metadata={"description": "Reference for testing anti-patterns"},
    )
    result = component.to_dict()
    assert result == {
        "type": "skill",
        "name": "testing-anti-patterns",
        "path": "/home/user/.claude/skills/testing-anti-patterns/SKILL.md",
        "hash": "abc123",
        "scope": "user",
        "locality": "local",
        "metadata": {"description": "Reference for testing anti-patterns"},
    }


def test_locality_is_local_for_disk_scopes() -> None:
    for scope in (Scope.MANAGED, Scope.USER, Scope.PROJECT, Scope.LOCAL, Scope.PLUGIN):
        c = Component(ComponentType.SKILL, "x", "/x", "h", scope)
        assert c.locality == Locality.LOCAL
        assert c.to_dict()["locality"] == "local"


def test_locality_is_remote_for_remote_scope() -> None:
    c = Component(ComponentType.MCP_SERVER, "x", "<remote>", "h", Scope.REMOTE)
    assert c.locality == Locality.REMOTE
    assert c.to_dict()["locality"] == "remote"


def test_component_to_dict_empty_metadata() -> None:
    component = Component(
        component_type=ComponentType.MCP_SERVER,
        name="alph",
        path="",
        content_hash="def456",
        scope=Scope.USER,
    )
    result = component.to_dict()
    assert result["metadata"] == {}


def test_hash_file_is_deterministic() -> None:
    with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False) as f:
        f.write("# Test content\nHello world")
        f.flush()
        path = Path(f.name)
    h1 = hash_file(path)
    h2 = hash_file(path)
    assert h1 == h2
    assert len(h1) == 64  # SHA-256 hex digest
    path.unlink()


def test_hash_directory_is_deterministic() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        base = Path(tmpdir)
        (base / "a.md").write_text("file a")
        (base / "b.md").write_text("file b")
        sub = base / "sub"
        sub.mkdir()
        (sub / "c.md").write_text("file c")

        h1 = hash_directory(base)
        h2 = hash_directory(base)
        assert h1 == h2
        assert len(h1) == 64


def test_hash_directory_changes_with_content() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        base = Path(tmpdir)
        (base / "a.md").write_text("version 1")
        h1 = hash_directory(base)
        (base / "a.md").write_text("version 2")
        h2 = hash_directory(base)
        assert h1 != h2


def test_sort_key_orders_by_type_scope_name_path() -> None:
    c1 = Component(ComponentType.SKILL, "z-skill", "/z", "h1", Scope.USER)
    c2 = Component(ComponentType.AGENT, "a-agent", "/a", "h2", Scope.PROJECT)
    c3 = Component(ComponentType.SKILL, "a-skill", "/a", "h3", Scope.USER)
    components = sorted([c1, c2, c3], key=sort_key)
    names = [c.name for c in components]
    assert names == ["a-agent", "a-skill", "z-skill"]


def test_inventory_to_json_is_deterministic() -> None:
    components = [
        Component(ComponentType.HOOK, "tdd-edit-guard", "/hooks/tdd.sh", "h1", Scope.USER),
        Component(ComponentType.AGENT, "tdd-guardian", "/agents/tdd.md", "h2", Scope.USER),
    ]
    j1 = inventory_to_json(components)
    j2 = inventory_to_json(components)
    assert j1 == j2
    parsed = json.loads(j1)
    types = [c["type"] for c in parsed["components"]]
    assert types == ["agent", "hook"]  # sorted by type
