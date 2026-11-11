"""Prepared actual-function tests; native disposable Git fixtures need separate GO."""
import ast
import dataclasses
import io
from pathlib import Path
from types import SimpleNamespace
from typing import Optional
import subprocess
import os
import shutil
import sys

import pytest

HERE = Path(__file__).resolve().parent
REFRESH_SOURCE = HERE / "dest_refresh.py" if (HERE / "dest_refresh.py").is_file() else HERE.parent / "dest_refresh.py"
PUBLISH_SOURCE = HERE / "publish.py" if (HERE / "publish.py").is_file() else HERE.parents[3] / "coordinator/bin/publish.py"
HEAD = "1" * 40
TIP = "2" * 40


def namespace(runner):
    source = ast.parse(REFRESH_SOURCE.read_text(encoding="utf-8"))
    names = {"RefreshResult", "preflight_dest_refresh", "fast_forward_dest", "_branch_and_upstream", "_no_upstream_base"}
    selected = [n for n in source.body if isinstance(n, (ast.ClassDef, ast.FunctionDef)) and n.name in names]
    ns = dict(Path=Path, Optional=Optional, TextIO=object, List=list,
              dataclass=dataclasses.dataclass, replace=dataclasses.replace,
              _git=runner, _refresh_local_main=lambda *a: None, io=io,
              default_remote_branch=lambda root: "origin/candidate")
    ns["_last_line"] = lambda value, default: value.strip().splitlines()[-1] if value.strip() else default
    ns["_resolve_commit"] = lambda root, ref: resolved(runner(root, ["rev-parse", "--verify", "--quiet", ref + "^{commit}"]))
    exec(compile(ast.Module(body=selected, type_ignores=[]), str(REFRESH_SOURCE), "exec"), ns)
    return ns


def resolved(result):
    return result.stdout.strip() or None if result.returncode == 0 else None


def fake_runner(*, dryrun_rc=0, head=HEAD, upstream=TIP, branch="candidate", tracking="origin/candidate"):
    calls = []
    def run(root, args):
        calls.append(args)
        if args == ["rev-parse", "--abbrev-ref", "HEAD", "@{u}"]:
            return SimpleNamespace(returncode=0 if tracking else 1, stdout=branch + "\n" + tracking if tracking else "", stderr="")
        if args == ["rev-parse", "--abbrev-ref", "HEAD"]:
            return SimpleNamespace(returncode=0, stdout=branch, stderr="")
        if args[0] == "rev-parse":
            value = head if args[-1] == "HEAD^{commit}" else upstream
            return SimpleNamespace(returncode=0 if value else 1, stdout=value or "", stderr="")
        return SimpleNamespace(returncode=dryrun_rc if args[0] == "read-tree" else 0, stdout="", stderr="local changes would be overwritten" if dryrun_rc else "")
    return run, calls


def result(ns, **kw):
    return ns["RefreshResult"](HERE, True, branch="candidate", upstream="origin/candidate",
                               behind=1, landing_tip=TIP, refresh_head=HEAD, **kw)


def test_native_dryrun_exact_oid_argv_and_no_mutating_command():
    run, calls = fake_runner()
    ns = namespace(run)
    checked = ns["preflight_dest_refresh"](result(ns))
    assert checked.ok
    assert ["read-tree", "--dry-run", "-m", "-u", HEAD, TIP] in calls
    assert not any(c[0] in {"merge", "reset", "checkout", "update-ref"} for c in calls)


def test_dirty_overlap_refuses_before_real_merge():
    run, calls = fake_runner(dryrun_rc=128)
    ns = namespace(run)
    checked = ns["fast_forward_dest"](result(ns), out=io.StringIO(), err=io.StringIO())
    assert not checked.ok and "precondition refused" in checked.reason
    assert not any(c[0] == "merge" for c in calls)


def test_head_drift_after_successful_preflight_refuses_at_mutation_boundary():
    run, calls = fake_runner()
    ns = namespace(run)
    checked = ns["preflight_dest_refresh"](result(ns))
    assert checked.ok
    ns["_resolve_commit"] = lambda root, ref: "3" * 40 if ref == "HEAD" else TIP
    refused = ns["fast_forward_dest"](checked, out=io.StringIO(), err=io.StringIO())
    assert not refused.ok and "changed" in refused.reason
    assert not any(c[0] == "merge" for c in calls)


def test_tracking_name_drift_same_oid_refuses_before_dryrun():
    run, calls = fake_runner(tracking="origin/other")
    ns = namespace(run)
    refused = ns["preflight_dest_refresh"](result(ns))
    assert not refused.ok
    assert not any(c[0] in {"read-tree", "merge"} for c in calls)


def test_tracking_name_drift_same_oid_refuses_again_before_mutation():
    run, calls = fake_runner()
    ns = namespace(run)
    checked = ns["preflight_dest_refresh"](result(ns))
    assert checked.ok
    ns["_branch_and_upstream"] = lambda root: ("candidate", "origin/other", None)
    refused = ns["fast_forward_dest"](checked, out=io.StringIO(), err=io.StringIO())
    assert not refused.ok
    assert not any(c[0] == "merge" for c in calls)


def test_no_tracking_uses_existing_default_remote_fallback():
    run, calls = fake_runner(tracking=None)
    ns = namespace(run)
    assert ns["preflight_dest_refresh"](result(ns)).ok
    assert ["read-tree", "--dry-run", "-m", "-u", HEAD, TIP] in calls


@pytest.mark.parametrize("change", [{"head": "3" * 40}, {"upstream": "4" * 40}, {"branch": "work/other"}, {"upstream": None}])
def test_fetch_identity_drift_refuses_before_dryrun(change):
    run, calls = fake_runner(**change)
    ns = namespace(run)
    assert not ns["preflight_dest_refresh"](result(ns)).ok
    assert not any(c[0] in {"merge", "read-tree"} for c in calls)


@pytest.mark.parametrize("field", ["pending_abort", "subsumed"])
def test_special_recovery_modes_preserved_not_simulated(field):
    run, calls = fake_runner()
    ns = namespace(run)
    original = result(ns, **{field: True if field == "pending_abort" else ("peer",)})
    assert ns["preflight_dest_refresh"](original) is original
    assert calls == []


def test_second_destination_refusal_occurs_before_any_quiesce():
    tree = ast.parse(PUBLISH_SOURCE.read_text(encoding="utf-8"))
    starts = [n for n in ast.walk(tree) if isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "_preflighted" for t in n.targets)]
    assert len(starts) == 1
    # Extract the real publisher's containing statement list and exact preflight block.
    owner = next(n for n in ast.walk(tree) if hasattr(n, "body") and isinstance(n.body, list) and starts[0] in n.body)
    index = owner.body.index(starts[0])
    quiesce = next(i for i, n in enumerate(owner.body) if isinstance(n, ast.With) and any(isinstance(item.context_expr, ast.Call) and isinstance(item.context_expr.func, ast.Name) and item.context_expr.func.id == "_quiesced_engine_roots" for item in n.items))
    assert quiesce > index + 2
    block = owner.body[index:index + 3]
    events = []
    def check(row):
        events.append(row)
        return SimpleNamespace(ok=row == "first", reason="overlap")
    ns = {"_fetched": ["first", "second"], "_preflight_dest_refresh": check,
          "sys": SimpleNamespace(stderr=io.StringIO())}
    fn = ast.FunctionDef(name="probe", args=ast.arguments(posonlyargs=[], args=[ast.arg(arg="_fetched")], kwonlyargs=[], kw_defaults=[], defaults=[]), body=block, decorator_list=[])
    exec(compile(ast.fix_missing_locations(ast.Module(body=[fn], type_ignores=[])), "actual-publisher-block", "exec"), ns)
    assert ns["probe"](["first", "second"]) == 1
    assert events == ["first", "second"]


def real_git():
    """Match git.run's Windows redirector resolution; require an absolute binary."""
    found = shutil.which("git")
    if found is None or not Path(found).is_absolute():
        pytest.skip("an absolute Git executable is not available on PATH")
    path = Path(found)
    if sys.platform == "win32" and path.parent.name.lower() == "cmd":
        candidates = [path.parent.parent / sub / "bin/git.exe" for sub in ("ucrt64", "mingw64", "clangarm64", "mingw32")]
        path = next((candidate for candidate in candidates if candidate.is_file()), None)
        if path is None: pytest.skip("real Git executable behind the Windows cmd redirector is missing")
    path = path.resolve()
    if not path.is_file() or path.suffix.lower() in {".cmd", ".bat"}:
        pytest.skip("a real Git executable is not available")
    return path
GIT_COMMANDS = {"init", "config", "add", "commit", "rev-parse", "branch", "switch", "merge-base", "read-tree"}
_GIT_SEQUENCE = 0


def native_environment(tmp_path, monkeypatch):
    if "P0_FIXTURE_RUN_DIR" not in os.environ:
        monkeypatch.setenv("P0_FIXTURE_RUN_DIR", str(tmp_path.resolve()))
    run = Path(os.environ["P0_FIXTURE_RUN_DIR"]).resolve()
    assert tmp_path.resolve().is_relative_to(run)
    config = tmp_path / "empty-global.gitconfig"
    config.write_bytes(b"")
    for key in list(os.environ):
        if key.upper().startswith("GIT_"): monkeypatch.delenv(key)
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(config))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_OPTIONAL_LOCKS", "0")
    monkeypatch.setenv("GIT_TERMINAL_PROMPT", "0")


@pytest.fixture
def native_git_environment(tmp_path, monkeypatch):
    native_environment(tmp_path, monkeypatch)


def git(root, *args, check=True):
    global _GIT_SEQUENCE
    executable = real_git()
    run = Path(os.environ["P0_FIXTURE_RUN_DIR"]).resolve()
    root = Path(root).resolve()
    assert root.is_relative_to(run) and args and args[0] in GIT_COMMANDS
    if args[0] == "init": assert not (root / ".git").exists() and not any(root.iterdir())
    else: assert (root / ".git").is_dir()
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith("GIT_")}
    env.update(GIT_CONFIG_GLOBAL=os.environ["GIT_CONFIG_GLOBAL"], GIT_CONFIG_NOSYSTEM="1",
               GIT_OPTIONAL_LOCKS="0", GIT_TERMINAL_PROMPT="0")
    _GIT_SEQUENCE += 1
    out = run / f"fixture-git-{_GIT_SEQUENCE}.stdout"
    err = run / f"fixture-git-{_GIT_SEQUENCE}.stderr"
    argv = [str(executable), "--no-pager", "-C", str(root), *args]
    with out.open("xb") as stdout, err.open("xb") as stderr:
        completed = subprocess.run(argv, executable=str(executable), check=False, stdin=subprocess.DEVNULL,
                                   stdout=stdout, stderr=stderr, env=env, timeout=15,
                                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    result = subprocess.CompletedProcess(argv, completed.returncode, out.read_text(), err.read_text())
    if check and result.returncode: raise subprocess.CalledProcessError(result.returncode, argv, result.stdout, result.stderr)
    return result


def test_native_fixture_without_driver_environment_is_owned_and_reproducible(tmp_path, monkeypatch):
    monkeypatch.delenv("P0_FIXTURE_RUN_DIR", raising=False)
    native_environment(tmp_path, monkeypatch)
    assert Path(os.environ["P0_FIXTURE_RUN_DIR"]) == tmp_path.resolve()
    repo = tmp_path / "without-driver"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "candidate")
    assert (repo / ".git").is_dir()
    assert git(repo, "rev-parse", "--abbrev-ref", "HEAD", check=False).returncode != 0


@pytest.mark.parametrize("collision", ["tracked", "untracked", "unrelated", "clean"])
def test_real_git_dryrun_preserves_index_worktree_and_head(tmp_path, collision, native_git_environment):
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "candidate")
    git(repo, "config", "user.email", "fixture@local")
    git(repo, "config", "user.name", "Fixture")
    (repo / "changed.txt").write_bytes(b"old\n")
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "base")
    head = git(repo, "rev-parse", "HEAD").stdout.strip()
    git(repo, "switch", "-c", "peer")
    (repo / "changed.txt").write_bytes(b"peer\n")
    (repo / "new.txt").write_bytes(b"new\n")
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "peer")
    tip = git(repo, "rev-parse", "HEAD").stdout.strip()
    git(repo, "switch", "candidate")
    git(repo, "branch", "--set-upstream-to=peer", "candidate")
    if collision == "tracked": (repo / "changed.txt").write_bytes(b"local\n")
    if collision == "untracked": (repo / "new.txt").write_bytes(b"local\n")
    if collision == "unrelated": (repo / "other.txt").write_bytes(b"keep\n")
    before = {p.relative_to(repo).as_posix(): p.read_bytes() for p in repo.rglob("*") if p.is_file() and ".git" not in p.relative_to(repo).parts}
    index = (repo / ".git/index").read_bytes()
    ns = namespace(lambda root, args: git(root, *args, check=False))
    row = ns["RefreshResult"](repo, True, branch="candidate", upstream="peer", behind=1, landing_tip=tip, refresh_head=head)
    checked = ns["preflight_dest_refresh"](row)
    assert checked.ok is (collision in {"clean", "unrelated"})
    assert git(repo, "rev-parse", "HEAD").stdout.strip() == head
    assert (repo / ".git/index").read_bytes() == index
    assert {p.relative_to(repo).as_posix(): p.read_bytes() for p in repo.rglob("*") if p.is_file() and ".git" not in p.relative_to(repo).parts} == before
    assert not (repo / ".git/index.lock").exists()
