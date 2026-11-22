"""The clean-overlay mirror holds only the run's test closure on a cold fill and a warm advance."""
from __future__ import annotations

import json
import subprocess
import sys

import pytest

from coordinator_core.ops.dispatch_emit import clean_overlay_verify as cov
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

RUN = "run1"
MOD_TEST = "tests/test_mod.py"
OTHER_TEST = "tests/test_other.py"


@pytest.fixture(autouse=True)
def _isolated_system_temp(tmp_path, monkeypatch):
    import tempfile

    (tmp_path / "systemp").mkdir()
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path / "systemp"))


def _git(root, *args):
    out = subprocess.run(
        ["git", "-C", str(root), "-c", "user.name=t", "-c", "user.email=t@t", *args],
        check=True, capture_output=True, text=True, **no_console_creationflags(),
    )
    return out.stdout.strip()


def _write(root, rel, text):
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


def _commit(root, message):
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", message)
    return _git(root, "rev-parse", "HEAD")


def _repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    _write(root, "pkg/__init__.py", "")
    _write(root, "pkg/mod.py", "VALUE = 1\n")
    _write(root, "pkg/other.py", "OTHER = 2\n")
    _write(root, MOD_TEST, "from pkg import mod\n\n\ndef test_mod():\n    assert mod.VALUE == 1\n")
    _write(root, OTHER_TEST, "from pkg import other\n\n\ndef test_other():\n    assert other.OTHER == 2\n")
    _write(root, "docs/unrelated.txt", "unrelated\n")
    _write(root, "docs/more/unrelated2.txt", "unrelated\n")
    _commit(root, "init")
    return root


def _dest(root):
    return cov.mirror_root(root) / root.resolve().name


def _indexed(root):
    return set(_git(_dest(root), "ls-files").splitlines())


def _export_run(root, *cmd):
    return cov.main(["--export-run", "--run-id", RUN, "--repo-root", str(root), "--", *cmd])


def _manifest(root, declared):
    path = cov.measured_rev_record_path(root, RUN).with_name("manifest.json")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"review_declared_paths": declared}), encoding="utf-8")


def _pytest(*targets):
    return [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", *targets]


@pytest.mark.parametrize("shape", ["unrelated", "changed", "module", "added"])
def test_export_refuses_only_a_name_absent_from_the_head_copy_of_a_dirty_peer_module(tmp_path, shape):
    root = _repo(tmp_path)
    importer = "coordinator_core/group_em/watch.py"
    target = "coordinator_core/hooks/assert_em_role.py"
    definition = "def _resolve_claude_config_dir():\n    return 'config'\n"
    _write(root, importer, "VALUE = None\n")
    _write(root, target, "HARDWARE = 'old'\n" + ("" if shape == "added" else definition))
    _commit(root, "fleet modules")
    _write(root, target, "HARDWARE = 'new'\n" +
           (definition.replace("'config'", "'changed'") if shape == "changed" else definition))
    statement = ("import coordinator_core.hooks.assert_em_role" if shape == "module"
                 else "from coordinator_core.hooks.assert_em_role import _resolve_claude_config_dir")
    _write(root, importer, statement + "\n")
    if shape == "added":
        with pytest.raises(cov.CoTenantDependency, match="co_tenant_dependency") as caught:
            cov._check_overlay_imports(root, [importer])
        assert (caught.value.importer, caught.value.peer, caught.value.symbol) == (
            importer, target, "_resolve_claude_config_dir")
    else:
        cov._check_overlay_imports(root, [importer])


@pytest.mark.parametrize("binding", [
    "A, B = 1, 2\n",
    "[A, *B] = 1, 2\n",
    "if True:\n    A = 1\nelse:\n    B = 2\n",
    "try:\n    A = 1\nexcept ImportError:\n    A = 2\n",
    "with open(__file__):\n    def A():\n        return 1\n",
])
def test_a_name_bound_by_unpacking_or_a_nested_block_in_head_is_not_refused(tmp_path, binding):
    root = _repo(tmp_path)
    _write(root, "pkg/peer.py", binding + "B = 3\n")
    _write(root, "pkg/mine.py", "VALUE = 0\n")
    _commit(root, "peer and importer")
    _write(root, "pkg/peer.py", binding + "B = 3\nNEW = 1\n")
    _write(root, "pkg/mine.py", "from pkg.peer import A\n")
    cov._check_overlay_imports(root, ["pkg/mine.py"])
    _write(root, "pkg/mine.py", "from pkg.peer import NEW\n")
    with pytest.raises(cov.CoTenantDependency) as caught:
        cov._check_overlay_imports(root, ["pkg/mine.py"])
    assert (caught.value.missing, caught.value.symbol, caught.value.module) == ("symbol", "NEW", "pkg.peer")
    assert "pkg.peer.NEW" in str(caught.value)


def test_a_module_absent_at_head_is_named_as_the_missing_module_not_the_first_import(tmp_path):
    root = _repo(tmp_path)
    _write(root, "pkg/mine.py", "VALUE = 0\n")
    _commit(root, "importer")
    _write(root, "pkg/fresh.py", "ALPHA = 1\nBETA = 2\n")
    _write(root, "pkg/mine.py", "from pkg.fresh import ALPHA, BETA\n")
    with pytest.raises(cov.CoTenantDependency) as caught:
        cov._check_overlay_imports(root, ["pkg/mine.py"])
    exc = caught.value
    assert (exc.missing, exc.module, exc.symbol, exc.peer) == ("module", "pkg.fresh", "pkg.fresh", "pkg/fresh.py")
    assert "needs pkg.fresh from pkg/fresh.py" in str(exc) and "ALPHA" not in str(exc)


def _peer_fixture(tmp_path, importer_text):
    root = _repo(tmp_path)
    _write(root, "pkg/peer.py", "def old():\n    return 1\n")
    _write(root, "pkg/mine.py", "VALUE = 0\n")
    _write(root, "tests/test_mine.py", "from pkg import mine\n\n\ndef test_mine():\n    assert mine.VALUE == 1\n")
    _commit(root, "peer and run files")
    head_peer = (root / "pkg/peer.py").read_bytes()
    _write(root, "pkg/peer.py", "def old():\n    return 1\n\n\ndef peer_only():\n    return 2\n")
    _write(root, "pkg/mine.py", importer_text)
    _manifest(root, ["pkg/mine.py"])
    return root, head_peer


def test_a_dirty_peer_import_the_run_edits_around_exports_at_its_head_bytes_and_runs_green(tmp_path):
    root, head_peer = _peer_fixture(tmp_path, "from pkg.peer import old\n\nVALUE = old()\n")

    assert _export_run(root, *_pytest("tests/test_mine.py")) == 0

    # the mirror checkout may apply the box's autocrlf; the blob is what must match
    assert (_dest(root) / "pkg/peer.py").read_bytes().replace(b"\r\n", b"\n") == head_peer
    assert (_dest(root) / "pkg/mine.py").read_text(encoding="utf-8").startswith("from pkg.peer import old")


def test_a_symbol_only_in_a_peers_uncommitted_dirt_is_a_typed_co_tenant_dependency(tmp_path, capsys):
    root, head_peer = _peer_fixture(tmp_path, "from pkg.peer import peer_only\n\nVALUE = peer_only() - 1\n")

    assert _export_run(root, *_pytest("tests/test_mine.py")) == 2

    err = capsys.readouterr().err
    assert "co_tenant_dependency" in err and "pkg/peer.py" in err and "peer_only" in err
    peer = _dest(root) / "pkg/peer.py"
    assert not peer.exists() or peer.read_bytes().replace(b"\r\n", b"\n") == head_peer


@pytest.mark.parametrize("change", ["add-registration", "remove-definition", "direct-import", "new-name", "clean-submodule"])
def test_export_holds_a_dirty_parent_package_at_head_and_refuses_only_a_missing_name(tmp_path, change):
    root = _repo(tmp_path)
    parent = "coordinator_core/ops/__init__.py"
    importer = "coordinator_core/bug_blitz/contract.py"
    committed = 'REGISTRATIONS = ("existing",)\n'
    _write(root, parent, committed)
    _write(root, "coordinator_core/ops/_path_guard.py", "def safe_id(value):\n    return value\n")
    _write(root, importer, "VALUE = None\n")
    test = "tests/test_contract.py"
    _write(root, test, "from coordinator_core.bug_blitz.contract import safe_id\n\n"
           "def test_safe_id():\n    assert safe_id('id') == 'id'\n")
    _commit(root, "package and importer")
    dirty = 'REGISTRATIONS = ("existing", "rulings.*", "group_em_fleet_capacity")\nNEW_ONLY = 1\n'
    _write(root, parent, "# registration removed\n" if change == "remove-definition" else dirty)
    guard = "from coordinator_core.ops._path_guard import safe_id\n"
    names = {"direct-import": "REGISTRATIONS", "new-name": "NEW_ONLY", "clean-submodule": "_path_guard"}
    _write(root, importer, (f"from coordinator_core.ops import {names[change]}\n" if change in names else "") + guard)
    _manifest(root, [importer])

    if change == "new-name":
        assert _export_run(root, *_pytest(test)) == 2
    else:
        assert _export_run(root, *_pytest(test)) == 0
        assert (_dest(root) / parent).read_text(encoding="utf-8") == committed


def test_scoped_export_refuses_an_unowned_dirty_import_dependency(tmp_path):
    root = _repo(tmp_path)
    _write(root, "pkg/mod.py", "from .other import NEW\nVALUE = NEW\n")
    _write(root, "pkg/other.py", "OTHER = 2\nNEW = 1\n")

    with pytest.raises(cov.ExportError, match="pkg/mod.py.*pkg/other.py"):
        with cov.clean_export(root, RUN, ["pkg/mod.py"], argv=[MOD_TEST]):
            pytest.fail("export yielded only half the import closure")

    with cov.clean_export(root, RUN, ["pkg/mod.py", "pkg/other.py"], argv=[MOD_TEST]) as dest:
        assert "NEW = 1" in (dest / "pkg" / "other.py").read_text(encoding="utf-8")


def test_scoped_export_refuses_a_removed_definition_with_a_committed_importer(tmp_path):
    root = _repo(tmp_path)
    _write(root, "pkg/mod.py", "def drift_reason():\n    return 'reason'\n")
    _write(root, "pkg/refusal.py", "from .mod import drift_reason\n")
    _write(root, MOD_TEST, "from pkg import refusal\n")
    _commit(root, "base importer")
    _write(root, "pkg/mod.py", "def new_reason():\n    return 'reason'\n")

    with pytest.raises(cov.ExportError, match=r"pkg/refusal\.py.*pkg/mod\.py.*drift_reason"):
        with cov.clean_export(root, RUN, ["pkg/mod.py"], argv=[MOD_TEST]):
            pytest.fail("export yielded an importer with a missing definition")

    _write(root, "pkg/refusal.py", "from .mod import new_reason\n")
    with cov.clean_export(root, RUN, ["pkg/mod.py", "pkg/refusal.py"], argv=[MOD_TEST]) as dest:
        assert "import new_reason" in (dest / "pkg" / "refusal.py").read_text(encoding="utf-8")


def test_export_run_with_a_test_argv_leaves_an_unrelated_tracked_file_out_of_disk_and_index(tmp_path):
    root = _repo(tmp_path)
    _manifest(root, [])

    assert _export_run(root, *_pytest(MOD_TEST)) == 0

    dest = _dest(root)
    assert (dest / "pkg" / "mod.py").is_file() and (dest / MOD_TEST).is_file()
    assert not (dest / "docs" / "unrelated.txt").exists()
    assert not (dest / "pkg" / "other.py").exists()
    assert "docs/unrelated.txt" not in _indexed(root) and "pkg/other.py" not in _indexed(root)


def test_state_records_the_closure_digest_beside_the_sha(tmp_path):
    root = _repo(tmp_path)
    _manifest(root, [])
    _export_run(root, *_pytest(MOD_TEST))

    state = json.loads((cov.mirror_root(root) / "state.json").read_text(encoding="utf-8"))

    assert state["at"] == _git(root, "rev-parse", "HEAD")
    assert state["closure_digest"] == cov._closure_digest(cov._mirror_closure(cov.mirror_root(root)))
    assert MOD_TEST in cov._mirror_closure(cov.mirror_root(root))


def test_a_second_run_naming_another_test_materialises_its_closure_on_the_warm_mirror(tmp_path):
    root = _repo(tmp_path)
    _manifest(root, [])
    _export_run(root, *_pytest(MOD_TEST))
    legs: list = []

    with cov.clean_export(root, "run2", [], leg=_leg_log(legs), argv=_pytest(OTHER_TEST)) as dest:
        assert (dest / "pkg" / "other.py").is_file() and (dest / OTHER_TEST).is_file()
        assert (dest / "pkg" / "mod.py").is_file()
        assert not (dest / "docs" / "unrelated.txt").exists()

    assert "clone" not in legs and "pkg/other.py" in _indexed(root)


def _leg_log(legs):
    from contextlib import contextmanager

    @contextmanager
    def leg(name):
        legs.append(name)
        yield {}

    return leg


def test_a_commit_changing_an_out_of_closure_file_leaves_it_absent_after_a_warm_advance(tmp_path):
    root = _repo(tmp_path)
    _manifest(root, [])
    _export_run(root, *_pytest(MOD_TEST))
    _write(root, "docs/unrelated.txt", "changed\n")
    _write(root, "docs/brand_new.txt", "new\n")
    _write(root, "pkg/mod.py", "VALUE = 1\n# edited\n")
    _git(root, "rm", "-q", "docs/more/unrelated2.txt")
    head = _commit(root, "second")

    assert _export_run(root, *_pytest(MOD_TEST)) == 0

    dest = _dest(root)
    assert (dest / "pkg" / "mod.py").read_text(encoding="utf-8").endswith("# edited\n")
    assert not (dest / "docs" / "unrelated.txt").exists() and not (dest / "docs" / "brand_new.txt").exists()
    assert _indexed(root) <= cov._mirror_closure(cov.mirror_root(root))
    assert cov._state_read(cov.mirror_root(root)) == head


def test_a_closure_path_new_at_the_advanced_sha_is_written(tmp_path):
    root = _repo(tmp_path)
    _manifest(root, [])
    _export_run(root, *_pytest(MOD_TEST))
    _write(root, "pkg/mod.py", "from pkg import extra\nVALUE = extra.EXTRA\n")
    _write(root, "pkg/extra.py", "EXTRA = 1\n")
    _commit(root, "grow")

    assert _export_run(root, *_pytest(MOD_TEST)) == 0

    assert (_dest(root) / "pkg" / "extra.py").is_file()


def test_a_bare_argv_fills_fully_and_says_so(tmp_path, capsys):
    root = _repo(tmp_path)
    _manifest(root, [])

    assert _export_run(root, sys.executable, "-c", "pass") == 0

    assert (_dest(root) / "docs" / "unrelated.txt").is_file() and (_dest(root) / "pkg" / "other.py").is_file()
    assert "full fill" in capsys.readouterr().err
    assert cov._mirror_closure(cov.mirror_root(root)) is None


def test_a_scoped_mirror_asked_for_a_full_export_is_rebuilt_full(tmp_path):
    root = _repo(tmp_path)
    _manifest(root, [])
    _export_run(root, *_pytest(MOD_TEST))
    assert not (_dest(root) / "docs" / "unrelated.txt").exists()

    assert _export_run(root, sys.executable, "-c", "pass") == 0

    assert (_dest(root) / "docs" / "unrelated.txt").is_file()
    assert cov._mirror_closure(cov.mirror_root(root)) is None


def test_a_closure_that_cannot_be_computed_is_a_full_fill_not_a_refusal(tmp_path, monkeypatch):
    from coordinator_core.ops.dispatch_emit import export_closure

    def boom(*_a, **_k):
        raise OSError("index unreadable")

    monkeypatch.setattr(export_closure, "test_closure", boom)
    root = _repo(tmp_path)
    _manifest(root, [])

    assert _export_run(root, *_pytest(MOD_TEST)) == 0
    assert (_dest(root) / "docs" / "unrelated.txt").is_file()


def test_verify_writes_only_the_seeds_closure_and_still_picks_a_test_importing_a_touched_module(tmp_path):
    root = _repo(tmp_path)
    _write(root, "tests/test_importer.py", "from pkg import mod\n\n\ndef test_importer():\n    assert mod.VALUE == 1\n")
    head = _commit(root, "importer")

    out = cov.verify(root, "run1", head, [MOD_TEST], touched=["pkg/mod.py"])

    assert out.verdict == "green", out
    assert any("tests/test_importer.py" in a for a in out.argv)
    dest = _dest(root)
    assert (dest / "tests" / "test_importer.py").is_file() and (dest / "pkg" / "mod.py").is_file()
    assert not (dest / "docs" / "unrelated.txt").exists() and not (dest / OTHER_TEST).exists()


def test_with_importers_over_a_tree_strips_the_sha_prefix(tmp_path):
    root = _repo(tmp_path)
    head = _git(root, "rev-parse", "HEAD")

    picked, note = cov._with_importers(root, (MOD_TEST,), ["pkg/other.py"], head)

    assert picked == (MOD_TEST, OTHER_TEST) and note == ""


def test_head_is_read_without_a_spawn_from_a_loose_a_packed_and_a_detached_ref(tmp_path, monkeypatch):
    root = _repo(tmp_path)
    head = _git(root, "rev-parse", "HEAD")
    monkeypatch.setattr(cov, "run_git", lambda *a, **k: (_ for _ in ()).throw(AssertionError("spawned")))

    assert cov._commit_sha(root, "HEAD") == head
    _git(root, "pack-refs", "--all", "--prune")
    assert cov._commit_sha(root, "HEAD") == head
    (root / ".git" / "HEAD").write_text(head + "\n", encoding="utf-8")
    assert cov._commit_sha(root, "HEAD") == head


def test_head_falls_back_to_rev_parse_when_the_ref_cannot_be_read(tmp_path):
    root = _repo(tmp_path)
    head = _git(root, "rev-parse", "HEAD")
    (root / ".git" / "HEAD").write_text("ref: refs/heads/gone\n", encoding="utf-8")

    assert cov._read_head(root) == ""
    with pytest.raises(cov.ExportError):
        cov._commit_sha(root, "HEAD")
    (root / ".git" / "HEAD").write_text(head + "\n", encoding="utf-8")
    assert cov._commit_sha(root, "HEAD") == head


def test_plumbing_runs_without_the_capped_job_and_a_pytest_run_keeps_it(tmp_path, monkeypatch):
    import coordinator_core.diagnostics.contained_run as contained

    root = _repo(tmp_path)
    jobs = []
    real = contained.spawn_in_capped_job
    monkeypatch.setattr(contained, "spawn_in_capped_job", lambda argv, **k: (jobs.append(argv[1:2]), real(argv, **k))[1])

    assert cov._run([cov._git_exe(), "ls-tree", "-z", "HEAD"], root, 30).returncode == 0
    assert jobs == []
    assert cov._run([cov._git_exe(), "read-tree", "--empty"], root, 30).returncode == 0
    assert jobs == [["read-tree"]]


def test_a_bare_word_in_argv_is_not_a_path_and_a_directory_and_a_node_id_are(tmp_path):
    root = _repo(tmp_path)

    assert not cov._names_a_path(root, "pytest") and not cov._names_a_path(root, "-q")
    assert cov._names_a_path(root, f"{MOD_TEST}::test_mod") and cov._names_a_path(root, "tests")
    assert cov._names_a_path(root, "pkg/mod.py")
    assert cov._names_a_path(root, "docs")
