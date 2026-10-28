
import subprocess
import time

import pytest

from coordinator_core.git import commit as gcommit
from coordinator_core.git.commit import CommitRefused

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_NOWIN = {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)}

_PORTABLE = (
    "claude-klabauter|mirror|publish-mirror:claude_klabauter|coordinator_core|"
    "coordinator_core||ops,DIRECTORY.md\n"
    "claude-klabauter-coordinator-bin|mirror|publish-mirror:x|coordinator/bin|"
    "coordinator/bin||existing_cli.py\n"
)

_DECLARATIONS = (
    "rows:\n"
    "  claude-klabauter:\n"
    "    deny:\n"
    "      - denied_name.py\n"
    "  claude-klabauter-coordinator-bin:\n"
    "    deny: []\n"
)


def _git(repo, *args, check=True):
    return subprocess.run(
        ["git", *args], cwd=str(repo), capture_output=True, text=True, check=check, **_NOWIN
    )


def _head(repo):
    return _git(repo, "rev-parse", "HEAD").stdout.strip()


@pytest.fixture()
def repo(tmp_path):
    r = tmp_path / "r"
    r.mkdir()
    _git(r, "init", "-q", "-b", "work/p")
    _git(r, "config", "user.email", "t@local")
    _git(r, "config", "user.name", "t")
    (r / "setup").mkdir()
    (r / "setup" / "publish-targets.portable").write_text(
        _PORTABLE, encoding="utf-8", newline="\n"
    )
    (r / "setup" / "publish-allowlist-declarations.yaml").write_text(
        _DECLARATIONS, encoding="utf-8", newline="\n"
    )
    (r / "coordinator_core").mkdir()
    (r / "coordinator_core" / "ops").mkdir()
    (r / "coordinator_core" / "ops" / "existing.py").write_text(
        "x = 1\n", encoding="utf-8", newline="\n"
    )
    (r / "keep.txt").write_text("keep\n", encoding="utf-8", newline="\n")
    _git(r, "add", "-A")
    _git(r, "commit", "-q", "-m", "seed")
    return r


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_ac2_undeclared_coordinator_core_name_is_refused(repo):
    before = _head(repo)

    with pytest.raises(CommitRefused) as excinfo:
        gcommit.commit_paths(
            repo, ["coordinator_core/new_module.py"], "add new module"
        )

    msg = str(excinfo.value)
    assert "new_module.py" in msg
    assert "claude-klabauter" in msg
    assert "publish-allowlist-generate.py" in msg
    assert "publish-targets.portable" in msg
    assert "publish-allowlist-declarations.yaml" in msg
    assert _head(repo) == before


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_ac3_undeclared_coordinator_bin_name_is_refused(repo):
    (repo / "coordinator").mkdir()
    (repo / "coordinator" / "bin").mkdir()
    before = _head(repo)

    with pytest.raises(CommitRefused) as excinfo:
        gcommit.commit_paths(repo, ["coordinator/bin/new_cli.py"], "add new cli")

    msg = str(excinfo.value)
    assert "new_cli.py" in msg
    assert "claude-klabauter-coordinator-bin" in msg
    assert "--pending coordinator/bin/new_cli.py" in msg
    assert _head(repo) == before


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_ac4_regenerated_field7_in_pathspec_commits_and_denied_name_commits(repo):
    (repo / "coordinator_core" / "new_module.py").write_text(
        "x = 1\n", encoding="utf-8", newline="\n"
    )
    regenerated = _PORTABLE.replace(
        "coordinator_core||ops,DIRECTORY.md",
        "coordinator_core||ops,DIRECTORY.md,new_module.py",
    )
    (repo / "setup" / "publish-targets.portable").write_text(
        regenerated, encoding="utf-8", newline="\n"
    )

    start = time.process_time()
    out = gcommit.commit_paths(
        repo,
        ["coordinator_core/new_module.py", "setup/publish-targets.portable"],
        "add and regenerate field7",
    )
    elapsed_ms = (time.process_time() - start) * 1000.0
    print(f"AC9 process time (triggered-and-passing, criterion 6 pending): {elapsed_ms:.3f}ms")
    assert out.sha
    assert elapsed_ms < 200.0

    (repo / "coordinator_core" / "denied_name.py").write_text(
        "x = 1\n", encoding="utf-8", newline="\n"
    )
    start = time.process_time()
    out2 = gcommit.commit_paths(
        repo, ["coordinator_core/denied_name.py"], "add denied name"
    )
    elapsed_ms2 = (time.process_time() - start) * 1000.0
    print(f"AC9 process time (yaml-reading deny arm): {elapsed_ms2:.3f}ms")
    assert out2.sha
    assert elapsed_ms2 < 200.0


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_ac5_declared_new_core_file_lands_without_allowlist_regeneration(repo):
    (repo / "coordinator_core" / "new_module.py").write_text(
        "x = 1\n", encoding="utf-8", newline="\n"
    )
    before = _head(repo)

    out = gcommit.commit_paths(
        repo, ["coordinator_core/new_module.py"], "declared new file, no regeneration"
    )

    assert out.sha and _head(repo) != before


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_ac5_declared_new_bin_file_lands_at_default_arguments(repo):
    (repo / "coordinator" / "bin").mkdir(parents=True)
    (repo / "coordinator" / "bin" / "new_cli.py").write_text(
        "x = 1\n", encoding="utf-8", newline="\n"
    )

    out = gcommit.commit_paths(repo, ["coordinator/bin/new_cli.py"], "declared new bin")

    assert out.sha


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_ac6_existing_top_level_name_commits_normally_and_deny_yaml_unread(repo, monkeypatch):
    (repo / "coordinator_core" / "ops" / "another.py").write_text(
        "x = 2\n", encoding="utf-8", newline="\n"
    )

    from coordinator_core.git import published_tree_classification as ptc

    original_deny_names = ptc.deny_names

    def _spy_deny_names(*a, **k):
        raise AssertionError("deny_names must not be read when the name is in field 7")

    monkeypatch.setattr(ptc, "deny_names", _spy_deny_names)

    out = gcommit.commit_paths(
        repo, ["coordinator_core/ops/another.py"], "existing top-level name"
    )

    assert out.sha
    monkeypatch.setattr(ptc, "deny_names", original_deny_names)


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_ac7_no_published_tree_paths_reads_nothing_for_this_check(repo, monkeypatch):
    from coordinator_core.git import published_tree_classification as ptc

    def _fail_unclassified(*a, **k):
        raise AssertionError("unclassified() must not run when nothing is touched")

    monkeypatch.setattr(ptc, "unclassified", _fail_unclassified)

    (repo / "keep.txt").write_text("edited\n", encoding="utf-8", newline="\n")
    out = gcommit.commit_paths(repo, ["keep.txt"], "unrelated edit")
    assert out.sha


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_ac7_deletion_only_published_tree_path_is_not_classified(repo, monkeypatch):
    from coordinator_core.git import published_tree_classification as ptc

    def _fail_unclassified(*a, **k):
        raise AssertionError("unclassified() must not run for a deletion-only path")

    monkeypatch.setattr(ptc, "unclassified", _fail_unclassified)

    (repo / "coordinator_core" / "ops" / "existing.py").unlink()
    out = gcommit.commit_paths(
        repo, [], "delete existing", deleted_paths=["coordinator_core/ops/existing.py"]
    )
    assert out.sha


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_ac7_repo_without_declarations_yaml_is_never_refused(tmp_path):
    r = tmp_path / "mirror"
    r.mkdir()
    _git(r, "init", "-q", "-b", "work/m")
    _git(r, "config", "user.email", "t@local")
    _git(r, "config", "user.name", "t")
    (r / "keep.txt").write_text("keep\n", encoding="utf-8", newline="\n")
    _git(r, "add", "-A")
    _git(r, "commit", "-q", "-m", "seed")

    (r / "coordinator_core").mkdir()
    (r / "coordinator_core" / "brand_new.py").write_text(
        "x = 1\n", encoding="utf-8", newline="\n"
    )
    out = gcommit.commit_paths(
        r, ["coordinator_core/brand_new.py"], "mirror, no declarations yaml"
    )
    assert out.sha


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_ac7_portable_present_declarations_missing_is_never_refused(repo):
    (repo / "setup" / "publish-allowlist-declarations.yaml").unlink()
    (repo / "coordinator_core" / "new_module.py").write_text(
        "x = 1\n", encoding="utf-8", newline="\n"
    )

    out = gcommit.commit_paths(
        repo,
        ["coordinator_core/new_module.py"],
        "declarations yaml missing, name unclassified",
    )
    assert out.sha


class _SpawnCounter:

    def __init__(self):
        self.argvs = []

    def __enter__(self):
        self._run, self._popen = subprocess.run, subprocess.Popen
        counter = self

        def run_spy(*a, **k):
            if a:
                counter.argvs.append(a[0])
            return counter._run(*a, **k)

        class PopenSpy(counter._popen):  # type: ignore[misc,valid-type]
            def __init__(self, *a, **k):
                if a:
                    counter.argvs.append(a[0])
                super().__init__(*a, **k)

        subprocess.run, subprocess.Popen = run_spy, PopenSpy
        return self

    def __exit__(self, *exc):
        subprocess.run, subprocess.Popen = self._run, self._popen
        return False


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_ac8_zero_spawns_on_the_refusal_path(repo):
    with _SpawnCounter() as counter:
        with pytest.raises(CommitRefused):
            gcommit.commit_paths(
                repo, ["coordinator_core/new_module.py"], "add new module"
            )
    assert counter.argvs == [], f"expected zero spawns, got {counter.argvs}"


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_ac8_zero_spawns_on_the_passing_and_deny_paths(repo):
    (repo / "coordinator_core" / "denied_name.py").write_text(
        "x = 1\n", encoding="utf-8", newline="\n"
    )
    with _SpawnCounter() as counter:
        out = gcommit.commit_paths(
            repo, ["coordinator_core/denied_name.py"], "add denied name"
        )
    assert out.sha
    assert counter.argvs == [], f"expected zero spawns, got {counter.argvs}"


def _load_generator():
    import importlib.util
    from pathlib import Path

    gen = Path(__file__).resolve().parents[3] / "coordinator" / "bin" / "publish-allowlist-generate.py"
    spec = importlib.util.spec_from_file_location("_pag_c6", gen)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_c6_new_bin_cli_lands_in_one_commit_via_pending(repo):
    (repo / "coordinator_core" / "denied_name.py").write_text(
        "x = 1\n", encoding="utf-8", newline="\n"
    )
    for rel in (
        "coordinator_core/bash_guards/m.py",
        "coordinator_core/hooks/m.py",
        "coordinator_core/session/m.py",
        "coordinator_core/write_guards/m.py",
        "coordinator_core/frontmatter/schema_validate.py",
        "coordinator_core/contract/cockpit_schema/emit_schema.py",
    ):
        f = repo / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text("x = 1\n", encoding="utf-8", newline="\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "track denied name and contract roots")
    (repo / "coordinator" / "bin").mkdir(parents=True)
    (repo / "coordinator" / "bin" / "new-tool.py").write_text(
        "x = 1\n", encoding="utf-8", newline="\n"
    )
    (repo / "coordinator" / "bin" / "new-tool.cmd").write_text(
        "@echo off\n", encoding="utf-8", newline="\n"
    )
    paths = [
        "coordinator/bin/new-tool.py",
        "coordinator/bin/new-tool.cmd",
        "setup/publish-targets.portable",
    ]

    gen = _load_generator()
    # No --pending: the untracked names are invisible to `git ls-files`,
    # yet the commit carries the files, so it lands.
    (repo / "setup" / "publish-targets.portable").write_text(
        gen.regenerate_portable(repo), encoding="utf-8", newline="\n"
    )
    assert gcommit.commit_paths(repo, paths, "bin cli without pending").sha

    (repo / "coordinator" / "bin" / "new-tool.py").write_text(
        "x = 2\n", encoding="utf-8", newline="\n"
    )
    (repo / "setup" / "publish-targets.portable").write_text(
        gen.regenerate_portable(repo, tuple(paths[:2])), encoding="utf-8", newline="\n"
    )
    out = gcommit.commit_paths(repo, paths, "bin cli with pending")
    assert out.sha
