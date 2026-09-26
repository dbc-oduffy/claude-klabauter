"""Characterization tests for coordinator_core.ops.verify_templates_setup_sync.

Built against the golden-oracle behavior of the DoE bash script, run
against a scratch CLAUDE_HOME/plugin-root pair — OK / MISMATCH /
LIVE_MISSING / TMPL_MISSING / NOT_PRESENT-for-all-pairs, plus the
exit-code arbitration rule. Extended (P077-C2) with the manifest-tracked
set, the source (repo-root) leg, and the publish_sync.py would-refuse
contract leg.

Port of: verify-templates-setup-sync.sh (DoE b5a4192c, 2026-07-20)
Spec: docs/plans/2026-09-11-pairs-oracle-reads-the-doe-source.md (C2)
"""
from __future__ import annotations

import sys
import textwrap
from pathlib import Path

import pytest

from coordinator_core.ops.verify_templates_setup_sync import (
    PluginRootUnresolved,
    _resolve_plugin_root,
    check_pairs,
    main,
)

_TRACKED = [
    "publish.sh",
    "publish_sync.py",
    "publish-targets.example.sh",
    ".percolate-identity.example",
    "percolate-hooks/README.md",
]

# PUBLISH_MODES entry point (sync_mirror, sync_flat_mirror, sync_repo_cut)
_CONTRACT_SATISFYING_PUBLISH_SYNC = textwrap.dedent(
    """
    def sync_mirror(copy_file, renamed_dir_names, sweep_top_level_orphans, renamed_file_names):
        pass

    def sync_flat_mirror(copy_file):
        pass

    def sync_repo_cut(dry_run):
        pass

    def load_ignore(root):
        pass
    """
)

_CONTRACT_REFUSING_PUBLISH_SYNC = textwrap.dedent(
    """
    def sync_mirror(renamed_dir_names, sweep_top_level_orphans, renamed_file_names):
        pass

    def sync_flat_mirror(copy_file):
        pass

    def sync_repo_cut(dry_run):
        pass

    def load_ignore(root):
        pass
    """
)

_CONTRACT_SATISFYING_WITH_EXTRAS = textwrap.dedent(
    """
    def _locate_percolate_lib():
        pass

    def sync_mirror(copy_file, renamed_dir_names, sweep_top_level_orphans, renamed_file_names, extra_knob=None, **kwargs):
        pass

    def sync_flat_mirror(copy_file, extra_knob=None, **kwargs):
        pass

    def sync_repo_cut(dry_run, extra_knob=None, **kwargs):
        pass

    def load_ignore(root, extra_knob=None, **kwargs):
        pass
    """
)


def _make_dirs(tmp_path: Path) -> tuple[Path, Path, Path]:
    templates = tmp_path / "templates" / "setup"
    live = tmp_path / "home" / ".claude" / "setup"
    source = tmp_path / "repo" / "setup"
    templates.mkdir(parents=True)
    live.mkdir(parents=True)
    return templates, live, source


def _write_tracked(dirpath: Path, relpaths, content_by_relpath=None) -> None:
    content_by_relpath = content_by_relpath or {}
    for relpath in relpaths:
        path = dirpath / relpath
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content_by_relpath.get(relpath, "same content\n"), encoding="utf-8")


@pytest.fixture(autouse=True)
def _percolate_on_path():
    coordinator_lib = Path(__file__).resolve().parents[2] / "coordinator" / "lib"
    inserted = str(coordinator_lib) not in sys.path
    if inserted:
        sys.path.insert(0, str(coordinator_lib))
    yield
    if inserted:
        sys.path.remove(str(coordinator_lib))


def test_all_ok_when_every_pair_byte_identical(tmp_path):
    templates, live, source = _make_dirs(tmp_path)
    _write_tracked(templates, _TRACKED, {"publish_sync.py": _CONTRACT_SATISFYING_PUBLISH_SYNC})
    _write_tracked(live, _TRACKED, {"publish_sync.py": _CONTRACT_SATISFYING_PUBLISH_SYNC})

    lines, rc = check_pairs(templates, live, _TRACKED, source_setup=None)
    assert rc == 0
    template_live_lines = [l for l in lines if l.startswith(("OK", "MISMATCH"))]
    assert len(template_live_lines) == len(_TRACKED)
    assert all(l.startswith("OK") for l in template_live_lines)


def test_mismatch_sets_nonzero_exit(tmp_path):
    templates, live, source = _make_dirs(tmp_path)
    _write_tracked(templates, _TRACKED, {"publish_sync.py": _CONTRACT_SATISFYING_PUBLISH_SYNC})
    _write_tracked(live, _TRACKED, {"publish_sync.py": _CONTRACT_SATISFYING_PUBLISH_SYNC})
    (live / "publish.sh").write_text("drifted live version\n")

    lines, rc = check_pairs(templates, live, _TRACKED, source_setup=None)
    assert rc == 1
    assert "MISMATCH     publish.sh" in lines


def test_live_missing_reported_and_nonzero_exit(tmp_path):
    templates, live, source = _make_dirs(tmp_path)
    (templates / "publish.sh").write_text("template only\n")

    lines, rc = check_pairs(templates, live, ["publish.sh"], source_setup=None)
    assert rc == 1
    assert any(line.startswith("LIVE_MISSING publish.sh") for line in lines)


def test_tmpl_missing_reported_and_nonzero_exit(tmp_path):
    templates, live, source = _make_dirs(tmp_path)
    (live / "publish.sh").write_text("live only\n")

    lines, rc = check_pairs(templates, live, ["publish.sh"], source_setup=None)
    assert rc == 1
    assert any(line.startswith("TMPL_MISSING publish.sh") for line in lines)


def test_neither_side_present_for_any_pair_is_graceful_zero_exit(tmp_path):
    templates, live, source = _make_dirs(tmp_path)

    lines, rc = check_pairs(templates, live, ["publish.sh"], source_setup=None)
    assert rc == 0
    template_live_lines = [l for l in lines if not l.startswith(("SOURCE_SKIPPED", "CONTRACT"))]
    assert all(
        line.startswith("NOT_PRESENT") or line.startswith("no files present")
        for line in template_live_lines
    )


def test_negative_stray_arg_warns_but_still_runs(tmp_path, monkeypatch, capsys):
    templates, live, source = _make_dirs(tmp_path)
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(tmp_path))
    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path / "home"))

    rc = main(["--fix"])
    captured = capsys.readouterr()
    assert "WARNING: verify-templates-setup-sync takes no flags" in captured.err
    assert rc == 0


def test_negative_no_args_no_warning(tmp_path, monkeypatch, capsys):
    templates, live, source = _make_dirs(tmp_path)
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(tmp_path))
    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path / "home"))

    rc = main([])
    captured = capsys.readouterr()
    assert captured.err == ""


def test_unset_plugin_root_raises_instead_of_falling_back_to_cwd(monkeypatch):
    monkeypatch.delenv("CLAUDE_PLUGIN_ROOT", raising=False)
    with pytest.raises(PluginRootUnresolved):
        _resolve_plugin_root()


def test_unset_plugin_root_makes_main_fail_loud_not_cwd_fallback(monkeypatch, capsys):
    monkeypatch.delenv("CLAUDE_PLUGIN_ROOT", raising=False)
    monkeypatch.setenv("CLAUDE_HOME", "/nonexistent/claude/home")

    rc = main([])
    captured = capsys.readouterr()
    assert rc == 1
    assert "CLAUDE_PLUGIN_ROOT is unset" in captured.err


def test_regression_stale_twin_copy_file_dropped_from_sync_mirror(tmp_path):
    templates, live, source = _make_dirs(tmp_path)
    tracked = ["publish_sync.py"]
    _write_tracked(
        templates, tracked, {"publish_sync.py": _CONTRACT_REFUSING_PUBLISH_SYNC}
    )
    _write_tracked(
        live, tracked, {"publish_sync.py": _CONTRACT_REFUSING_PUBLISH_SYNC}
    )

    lines, rc = check_pairs(templates, live, tracked, source_setup=None)
    assert rc == 1
    refuse_lines = [l for l in lines if l.startswith("CONTRACT_REFUSE")]
    assert refuse_lines, lines
    joined = " ".join(refuse_lines)
    assert "publish_sync.py" in joined
    assert "sync_mirror" in joined
    assert "copy_file" in joined


def test_repo_root_copy_refused_names_repo_root(tmp_path):
    templates, live, source = _make_dirs(tmp_path)
    source.mkdir(parents=True)
    tracked = ["publish_sync.py"]
    _write_tracked(
        templates, tracked, {"publish_sync.py": _CONTRACT_SATISFYING_PUBLISH_SYNC}
    )
    _write_tracked(
        live, tracked, {"publish_sync.py": _CONTRACT_SATISFYING_PUBLISH_SYNC}
    )
    _write_tracked(
        source, tracked, {"publish_sync.py": _CONTRACT_REFUSING_PUBLISH_SYNC}
    )

    lines, rc = check_pairs(templates, live, tracked, source_setup=source)
    assert rc == 1
    refuse_lines = [l for l in lines if l.startswith("CONTRACT_REFUSE") and "repo-root" in l]
    assert refuse_lines, lines


def test_no_false_positive_on_arbitrary_body_differences(tmp_path):
    templates, live, source = _make_dirs(tmp_path)
    source.mkdir(parents=True)
    tracked = ["publish_sync.py"]
    _write_tracked(
        templates, tracked, {"publish_sync.py": _CONTRACT_SATISFYING_PUBLISH_SYNC}
    )
    _write_tracked(
        live, tracked, {"publish_sync.py": _CONTRACT_SATISFYING_PUBLISH_SYNC}
    )
    _write_tracked(
        source, tracked, {"publish_sync.py": _CONTRACT_SATISFYING_WITH_EXTRAS}
    )

    lines, rc = check_pairs(templates, live, tracked, source_setup=source)
    assert rc == 0
    assert not any(l.startswith("CONTRACT_REFUSE") for l in lines)
    assert not any(l.startswith("SOURCE_MISMATCH") and "publish_sync.py" in l for l in lines)


def test_no_source_tree_skips_source_legs_but_template_contract_still_runs(tmp_path):
    templates, live, source = _make_dirs(tmp_path)
    tracked = ["publish_sync.py"]
    _write_tracked(
        templates, tracked, {"publish_sync.py": _CONTRACT_REFUSING_PUBLISH_SYNC}
    )
    _write_tracked(
        live, tracked, {"publish_sync.py": _CONTRACT_REFUSING_PUBLISH_SYNC}
    )

    lines, rc = check_pairs(templates, live, tracked, source_setup=source)
    skip_lines = [l for l in lines if l.startswith("SOURCE_SKIPPED")]
    assert len(skip_lines) == 1
    assert not any(l.startswith("SOURCE_") and not l.startswith("SOURCE_SKIPPED") for l in lines)
    assert rc == 1
    assert any(l.startswith("CONTRACT_REFUSE") and "template" in l for l in lines)


def test_template_canonical_file_with_no_repo_root_copy_does_not_fail(tmp_path):
    templates, live, source = _make_dirs(tmp_path)
    source.mkdir(parents=True)
    tracked = [".percolate-identity.example"]
    _write_tracked(templates, tracked)
    _write_tracked(live, tracked)

    lines, rc = check_pairs(templates, live, tracked, source_setup=source)
    assert rc == 0
    assert not any(l.startswith("SOURCE_MISMATCH") for l in lines)


def test_source_mismatch_on_non_publish_sync_file_is_byte_compared_and_fails(tmp_path):
    templates, live, source = _make_dirs(tmp_path)
    source.mkdir(parents=True)
    tracked = ["percolate-hooks/README.md"]
    _write_tracked(templates, tracked, {"percolate-hooks/README.md": "template body\n"})
    _write_tracked(source, tracked, {"percolate-hooks/README.md": "drifted repo-root body\n"})

    lines, rc = check_pairs(templates, live, tracked, source_setup=source)
    assert rc == 1
    assert any(
        l.startswith("SOURCE_MISMATCH") and "percolate-hooks/README.md" in l for l in lines
    )


def test_tracked_set_equals_union_of_manifest_attrs():
    from coordinator_core.install.setup_template_manifest import _MANIFEST_ATTRS
    from coordinator_core.ops.verify_templates_setup_sync import _tracked_relpaths
    from coordinator_core.engine_root import coordinator_engine_root_with_class
    import coordinator_core.install.setup_template_manifest as stm

    claude_klabauter_root, _cls = coordinator_engine_root_with_class()
    files, exec_files, hook_files = stm._load_setup_template_manifest(Path(claude_klabauter_root))
    expected = list(files) + list(exec_files) + list(hook_files)

    actual = _tracked_relpaths(Path(claude_klabauter_root))
    assert actual == expected
    assert len(_MANIFEST_ATTRS) == 3

    retired_names = [
        "publish.sh",
        "publish-targets.example.sh",
        "percolate-hooks/_lib/depersonalize-bin-resolve.sh",
        "percolate-hooks/coordinator-claude/post-rsync/10-transform.sh",
        "percolate-hooks/coordinator-claude-publish-repo-docs/post-rsync/10-transform.sh",
        "percolate-hooks/coordinator-claude-publish-repo-setup/post-rsync/10-transform.sh",
        "percolate-hooks/coordinator-claude-publish-repo-toplevel/post-rsync/10-transform.sh",
        "percolate-hooks/coordinator-claude-toplevel-wiki/post-rsync/20-transform.sh",
    ]
    for name in retired_names:
        assert name not in actual, name
