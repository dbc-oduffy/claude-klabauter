"""`_cmd_round` refuses before the publish check and any staging when the source ref's committed op
registry names a module its tree lacks. Real git source repo under tmp_path; objects are read
in-process, so the refusal costs no interpreter start."""
from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path

import pytest

from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

REAL_FOLD_BAR = True  # opts out of conftest's fixed fake shipped sha; this module needs the real rev-parse
REAL_REGISTRY_INTEGRITY = True  # opts out of conftest's stubbed registry predicate

_BIN_DIR = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("percolate_round_registry_integrity", _BIN_DIR / "percolate-round.py")
_mod = importlib.util.module_from_spec(_spec)  # type: ignore[arg-type]
_spec.loader.exec_module(_mod)  # type: ignore[union-attr]

_MODULE = "coordinator_core.ops.widget"
_FILES = {
    "coordinator_core/authz/classification.py": "OP_CLASSIFICATION = {'widget.run': 1}\n",
    "coordinator_core/op_scopes.py": "_OP_KEY_SCOPE = {'widget.run': 'x'}\n",
    "coordinator_core/ops/_registry_map.py": f"OP_MODULE_MAP = {{'widget.run': '{_MODULE}'}}\n",
    "coordinator_core/ops/__init__.py": f"_EAGER_OP_MODULES = [('{_MODULE}', 'x')]\n",
    "coordinator_core/authz/registration_quad.py": (
        "_KNOWN_UNCLASSIFIED_OPS_DEBT = frozenset()\n_KNOWN_INCOMPLETE_REGISTRATIONS = {}\n"
    ),
}


def _git(args, cwd) -> None:
    subprocess.run(
        ["git", *args], cwd=str(cwd), check=True, capture_output=True, text=True, **no_console_creationflags()
    )


def _source(tmp_path: Path, *, with_module: bool) -> Path:
    repo = tmp_path / "source"
    repo.mkdir()
    _git(["init", "-q"], repo)
    _git(["config", "user.email", "t@t.example"], repo)
    _git(["config", "user.name", "t"], repo)
    files = dict(_FILES)
    if with_module:
        files["coordinator_core/ops/widget.py"] = "X = 1\n"
    for rel, text in files.items():
        path = repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    _git(["add", "--", "."], repo)
    _git(["commit", "-q", "-m", "seed"], repo)
    return repo


def _run(monkeypatch, tmp_path, source: Path, *extra: str):
    reached = []
    published = []
    (tmp_path / "root").mkdir(exist_ok=True)
    monkeypatch.setattr(_mod, "_bootstrap_engine", lambda: None)
    monkeypatch.setattr(_mod, "_fold_bar_refusal", lambda *a, **k: None)
    monkeypatch.setattr(_mod, "_resolve_percolate_root", lambda override: str(tmp_path / "root"))
    monkeypatch.setattr(_mod, "_branch0_gate", lambda target, root: str(source))
    monkeypatch.setattr(_mod, "_resolve_dest", lambda target, root: str(tmp_path / "dest"))
    monkeypatch.setattr(_mod, "_field7_preflight", lambda *a, **k: None)
    monkeypatch.setattr(_mod, "_cmd_round_default", lambda *a, **k: reached.append(a) or 0)
    import coordinator_core.percolate.source_preflight as preflight

    monkeypatch.setattr(preflight, "check_source_published", lambda *a, **k: published.append(a) or None)
    args = _mod._build_parser().parse_args(["alpha", "--percolate-root", str(tmp_path / "root"), *extra])
    return _mod._cmd_round(args), reached, published


def test_registry_naming_an_absent_module_refuses_before_publish_and_staging(monkeypatch, tmp_path, capsys):
    source = _source(tmp_path, with_module=False)

    rc, reached, published = _run(monkeypatch, tmp_path, source)

    err = capsys.readouterr().err
    assert rc == _mod._EXIT_FAIL
    assert "registry-incomplete" in err and _MODULE in err
    assert reached == []
    assert published == []


def test_complete_registry_reaches_the_round(monkeypatch, tmp_path):
    source = _source(tmp_path, with_module=True)

    rc, reached, published = _run(monkeypatch, tmp_path, source)

    assert rc == 0
    assert len(reached) == 1 and len(published) == 1


def test_untracked_module_in_the_worktree_does_not_count(monkeypatch, tmp_path, capsys):
    source = _source(tmp_path, with_module=False)
    (source / "coordinator_core" / "ops" / "widget.py").write_text("X = 1\n", encoding="utf-8")

    rc, reached, _ = _run(monkeypatch, tmp_path, source)

    assert rc == _mod._EXIT_FAIL
    assert _MODULE in capsys.readouterr().err
    assert reached == []


def test_source_ref_is_judged_not_head(monkeypatch, tmp_path):
    source = _source(tmp_path, with_module=True)
    good = _mod._resolve_shipped_sha(source, "HEAD")
    (source / "coordinator_core" / "ops" / "widget.py").unlink()
    _git(["add", "-A", "--", "."], source)
    _git(["commit", "-q", "-m", "drop module"], source)

    rc, reached, _ = _run(monkeypatch, tmp_path, source, "--source-ref", good)

    assert rc == 0
    assert len(reached) == 1


def test_unresolvable_ref_refuses_with_its_reason(monkeypatch, tmp_path, capsys):
    source = _source(tmp_path, with_module=True)

    rc, reached, published = _run(monkeypatch, tmp_path, source, "--source-ref", "no-such-ref")

    err = capsys.readouterr().err
    assert rc == _mod._EXIT_FAIL
    assert "registry-incomplete" in err and "no-such-ref" in err
    assert reached == [] and published == []


def test_unreadable_blob_refuses_never_passes(monkeypatch, tmp_path):
    source = _source(tmp_path, with_module=True)
    sha = _mod._resolve_shipped_sha(source, "HEAD")
    from coordinator_core.git import git_objects

    monkeypatch.setattr(git_objects, "read_object", lambda common, blob: None)

    refusal = _mod._registry_integrity_refusal(source, "", sha)

    assert refusal is not None and "registry-incomplete" in refusal and "unreadable" in refusal


def test_round_resolves_the_shipped_sha_once(monkeypatch, tmp_path):
    source = _source(tmp_path, with_module=True)
    calls = []
    real = _mod._resolve_shipped_sha
    monkeypatch.setattr(_mod, "_resolve_shipped_sha", lambda root, ref: calls.append(ref) or real(root, ref))

    _run(monkeypatch, tmp_path, source)

    assert calls == ["HEAD"]
