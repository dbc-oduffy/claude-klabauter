"""rm_family verification names derive from the engine tree's resolver by shape."""
from pathlib import Path

from coordinator_core.install import substrate


def _tree(root: Path, family: str, module: str) -> Path:
    d = root / "coordinator" / "lib" / family
    d.mkdir(parents=True)
    (d / f"{module}.py").write_text("", encoding="utf-8")
    return root


def test_pre_rename_spelling(tmp_path):
    root = _tree(tmp_path, "resolve-claude-klabauter", "_resolve_claude_klabauter")
    assert substrate.rm_family_names(root) == ("_resolve_claude_klabauter.py",)


def test_renamed_spelling_is_not_the_hardcoded_name(tmp_path):
    root = _tree(tmp_path, "resolve-klabauter", "_resolve_klabauter")
    names = substrate.rm_family_names(root)
    assert names == ("_resolve_klabauter.py",)
    assert "_resolve_claude_klabauter.py" not in names


def test_not_found_returns_empty_and_message_names_root(tmp_path):
    assert substrate.rm_family_names(tmp_path) == ()
    assert str(tmp_path) in substrate.rm_family_not_found_message(tmp_path)


def test_ambiguous_shape_is_not_guessed(tmp_path):
    _tree(tmp_path, "resolve-a", "_resolve_a")
    _tree(tmp_path, "resolve-b", "_resolve_b")
    assert substrate.rm_family_names(tmp_path) == ()


class _StubManifest:
    def install_bin_resolvers_entries(self):
        return ()


def test_static_family_names_carry_derived_shim_not_constant(tmp_path, monkeypatch):
    root = _tree(tmp_path, "resolve-klabauter", "_resolve_klabauter")
    monkeypatch.setattr(substrate, "_load_bin_templates_manifest", lambda r: _StubManifest())
    names = substrate._static_bin_family_names(root)
    assert "_resolve_klabauter.py" in names
    assert "_resolve_claude_klabauter.py" not in names


def test_settings_home_shim_member_uses_renamed_engine_shim(tmp_path):
    from coordinator_core.install.settings_home_report import resolver_shim_members

    root = _tree(tmp_path / "engine", "resolve-klabauter", "_resolve_klabauter")
    sh = tmp_path / "sh"
    (sh / "bin").mkdir(parents=True)
    (sh / "bin" / "_resolve_klabauter.py").write_text("x")
    (member,) = resolver_shim_members(sh, root)
    assert member.present and member.required
    assert member.path.name == "_resolve_klabauter.py"


def test_settings_home_shim_member_absent_when_published_name_not_installed(tmp_path):
    from coordinator_core.install.settings_home_report import resolver_shim_members

    root = _tree(tmp_path / "engine", "resolve-klabauter", "_resolve_klabauter")
    sh = tmp_path / "sh"
    (sh / "bin").mkdir(parents=True)
    (sh / "bin" / "_resolve_claude_klabauter.py").write_text("x")
    (member,) = resolver_shim_members(sh, root)
    assert not member.present


def test_settings_home_shim_member_unresolvable_tree_fails_with_reason(tmp_path):
    from coordinator_core.install.settings_home_report import resolver_shim_members

    (member,) = resolver_shim_members(tmp_path / "sh", tmp_path)
    assert not member.present and member.required
    assert str(tmp_path) in member.label


def test_settings_home_shim_member_no_engine_root_is_unanswerable(tmp_path):
    from coordinator_core.install.settings_home_report import resolver_shim_members

    assert resolver_shim_members(tmp_path, None) == []
