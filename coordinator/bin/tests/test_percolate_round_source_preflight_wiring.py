"""`_cmd_round` refuses before staging anything when the source HEAD is not
pushed. Real gate (REAL_SOURCE_PREFLIGHT opts out of conftest's
stub), real git source, local bare origin; tmp_path only."""
from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path

import pytest

from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

REAL_SOURCE_PREFLIGHT = True

_BIN_DIR = Path(__file__).resolve().parent.parent


def _load_module():
    spec = importlib.util.spec_from_file_location("percolate_round_source_preflight", _BIN_DIR / "percolate-round.py")
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


_mod = _load_module()


def _git(args, cwd) -> None:
    subprocess.run(
        ["git", *args], cwd=str(cwd), check=True, capture_output=True, text=True, **no_console_creationflags()
    )


def _source_with_origin(tmp_path: Path) -> Path:
    bare = tmp_path / "origin.git"
    _git(["init", "-q", "--bare", str(bare)], tmp_path)
    repo = tmp_path / "source"
    repo.mkdir()
    _git(["init", "-q"], repo)
    _git(["config", "user.email", "t@t.example"], repo)
    _git(["config", "user.name", "t"], repo)
    (repo / "a.py").write_text("a = 1\n", encoding="utf-8")
    _git(["add", "--", "a.py"], repo)
    _git(["commit", "-q", "-m", "seed"], repo)
    _git(["branch", "-m", "work/wiring"], repo)
    _git(["remote", "add", "origin", str(bare)], repo)
    _git(["push", "-q", "-u", "origin", "work/wiring"], repo)
    return repo


def _stamp_mirror_at(tmp_path: Path, source: Path) -> None:
    """Mirror whose last publish stamped the source's own HEAD: nothing to roll back."""
    _stamp_mirror_with_foreign_head(tmp_path, _out(["rev-parse", "--short=12", "HEAD"], source))


def _run(monkeypatch, tmp_path, source: Path, *extra: str):
    reached = []
    (tmp_path / "root").mkdir(exist_ok=True)
    monkeypatch.setattr(_mod, "_resolve_percolate_root", lambda override: str(tmp_path / "root"))
    monkeypatch.setattr(_mod, "_branch0_gate", lambda target, root: str(source))
    monkeypatch.setattr(_mod, "_resolve_dest", lambda target, root: str(tmp_path / "dest"))
    monkeypatch.setattr(_mod, "_cmd_round_default", lambda *a, **k: reached.append(a) or 0)
    args = _mod._build_parser().parse_args(["alpha", "--percolate-root", str(tmp_path / "root"), *extra])
    return _mod._cmd_round(args), reached


def test_unpushable_source_refuses_before_the_round(monkeypatch, tmp_path, capsys):
    source = _source_with_origin(tmp_path)
    _git(["checkout", "-q", "-b", "work/no-upstream"], source)

    rc, reached = _run(monkeypatch, tmp_path, source)

    assert rc == _mod._EXIT_FAIL
    assert reached == []
    assert "has no upstream" in capsys.readouterr().err


def test_pushed_source_reaches_the_round(monkeypatch, tmp_path):
    source = _source_with_origin(tmp_path)
    _stamp_mirror_at(tmp_path, source)

    rc, reached = _run(monkeypatch, tmp_path, source)

    assert rc == 0
    assert len(reached) == 1


def _stamp_mirror_with_foreign_head(tmp_path: Path, head: str = "deadbeefcafe") -> None:
    dest = tmp_path / "dest"
    dest.mkdir()
    _git(["init", "-q"], dest)
    _git(["config", "user.email", "t@t.example"], dest)
    _git(["config", "user.name", "t"], dest)
    (dest / "f.txt").write_text("x", encoding="utf-8")
    _git(["add", "--", "f.txt"], dest)
    _git(["commit", "-q", "-m", f"publish [source-head {head}]"], dest)


def test_source_lacking_the_mirror_stamp_refuses_before_the_round(monkeypatch, tmp_path, capsys):
    source = _source_with_origin(tmp_path)
    _stamp_mirror_with_foreign_head(tmp_path)

    rc, reached = _run(monkeypatch, tmp_path, source)

    assert rc == _mod._EXIT_FAIL
    assert reached == []
    assert "deadbeefcafe" in capsys.readouterr().err


def test_unstamped_mirror_refuses_before_the_round(monkeypatch, tmp_path, capsys):
    source = _source_with_origin(tmp_path)

    rc, reached = _run(monkeypatch, tmp_path, source)

    assert rc == _mod._EXIT_FAIL
    assert reached == []
    assert "carries no [source-head <sha>] stamp" in capsys.readouterr().err


def test_allow_source_rollback_flag_reaches_the_round(monkeypatch, tmp_path):
    source = _source_with_origin(tmp_path)
    _stamp_mirror_with_foreign_head(tmp_path)

    rc, reached = _run(monkeypatch, tmp_path, source, "--allow-source-rollback")

    assert rc == 0
    assert len(reached) == 1


def _out(args, cwd) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), check=True, capture_output=True, text=True, **no_console_creationflags()
    ).stdout.strip()


def _shared_and_clone(tmp_path: Path):
    shared = tmp_path / "shared"
    shared.mkdir()
    _git(["init", "-q"], shared)
    _git(["config", "user.email", "t@t.example"], shared)
    _git(["config", "user.name", "t"], shared)
    (shared / "a.py").write_text("a = 1\n", encoding="utf-8")
    _git(["add", "--", "a.py"], shared)
    _git(["commit", "-q", "-m", "seed"], shared)
    _git(["branch", "-m", "work/shared"], shared)
    clone = tmp_path / "clone"
    _git(["clone", "-q", str(shared), str(clone)], tmp_path)
    _git(["config", "user.email", "t@t.example"], clone)
    _git(["config", "user.name", "t"], clone)
    _git(["checkout", "-q", "-b", "round1-merge"], clone)
    (clone / "b.py").write_text("b = 1\n", encoding="utf-8")
    _git(["add", "--", "b.py"], clone)
    _git(["commit", "-q", "-m", "merge"], clone)
    return shared, clone, _out(["rev-parse", "HEAD"], clone)


def test_private_clone_commit_on_candidate_source_ref_reaches_the_round(monkeypatch, tmp_path):
    shared, clone, sha = _shared_and_clone(tmp_path)
    _stamp_mirror_at(tmp_path, clone)
    _git(["config", "receive.denyCurrentBranch", "ignore"], shared)
    _git(["push", "-q", "origin", f"{sha}:refs/heads/candidate-source"], clone)
    before = _out(["rev-parse", "work/shared"], shared)

    rc, reached = _run(monkeypatch, tmp_path, clone, "--source-ref", sha)

    assert rc == 0
    assert len(reached) == 1
    assert _out(["rev-parse", "work/shared"], shared) == before
    assert _out(["symbolic-ref", "HEAD"], shared) == "refs/heads/work/shared"
    assert subprocess.run(
        ["git", "config", "branch.round1-merge.remote"], cwd=str(clone), capture_output=True, text=True,
        **no_console_creationflags(),
    ).returncode != 0
    _git(["cat-file", "-e", f"{sha}^{{commit}}"], shared)


def test_unpublished_source_ref_refuses_before_the_round(monkeypatch, tmp_path, capsys):
    _shared, clone, sha = _shared_and_clone(tmp_path)
    _stamp_mirror_at(tmp_path, clone)

    rc, reached = _run(monkeypatch, tmp_path, clone, "--source-ref", sha)

    assert rc == _mod._EXIT_FAIL
    assert reached == []
    assert "candidate-source" in capsys.readouterr().err


def test_source_ref_lacking_the_mirror_stamp_refuses(monkeypatch, tmp_path, capsys):
    shared, clone, sha = _shared_and_clone(tmp_path)
    _git(["config", "receive.denyCurrentBranch", "ignore"], shared)
    _git(["push", "-q", "origin", f"{sha}:refs/heads/candidate-source"], clone)
    _stamp_mirror_with_foreign_head(tmp_path)

    rc, reached = _run(monkeypatch, tmp_path, clone, "--source-ref", sha)

    assert rc == _mod._EXIT_FAIL
    assert reached == []
    assert "deadbeefcafe" in capsys.readouterr().err


def _stub_withholding(monkeypatch, verdict):
    from coordinator_core.percolate import source_preflight

    monkeypatch.setattr(source_preflight, "source_withholding", lambda *a, **k: verdict)


def test_withholding_refusal_refuses_before_the_round(monkeypatch, tmp_path, capsys):
    from coordinator_core.percolate.source_preflight import Withholding

    source = _source_with_origin(tmp_path)
    _stamp_mirror_at(tmp_path, source)
    _stub_withholding(monkeypatch, Withholding(refusal="overlap on y.py"))

    rc, reached = _run(monkeypatch, tmp_path, source)

    assert rc == _mod._EXIT_FAIL
    assert reached == []
    assert "percolate-round: refused: overlap on y.py" in capsys.readouterr().err


def _record_probe_calls(monkeypatch, calls: list) -> None:
    """Every later stage is stubbed to record: any `_run` argv, the early engine-import
    probe (raising=False: absent until the wiring row lands), and the round body."""
    monkeypatch.setattr(_mod, "_run", lambda cmd, **k: calls.append(("_run", list(cmd))) or 0)
    monkeypatch.setattr(
        _mod, "_early_engine_import_refusal", lambda *a, **k: calls.append(("early_import", a)), raising=False
    )


def _names_publish(calls: list) -> bool:
    return any(kind == "_run" and any("publish.py" in str(part) for part in payload) for kind, payload in calls)


def test_withholding_refusal_returns_before_any_publish_argv_or_import_probe(monkeypatch, tmp_path, capsys):
    from coordinator_core.percolate.source_preflight import Withholding

    source = _source_with_origin(tmp_path)
    _stamp_mirror_at(tmp_path, source)
    _stub_withholding(monkeypatch, Withholding(refusal="overlap on y.py"))
    calls: list = []
    _record_probe_calls(monkeypatch, calls)

    rc, reached = _run(monkeypatch, tmp_path, source)

    assert rc == _mod._EXIT_FAIL
    assert reached == []
    assert calls == []
    assert not _names_publish(calls)
    assert "percolate-round: refused: overlap on y.py" in capsys.readouterr().err


def test_unpublished_source_refusal_returns_before_withholding_is_computed(monkeypatch, tmp_path):
    from coordinator_core.percolate import source_preflight

    source = _source_with_origin(tmp_path)
    _git(["checkout", "-q", "-b", "work/no-upstream"], source)
    computed: list = []
    monkeypatch.setattr(source_preflight, "source_withholding", lambda *a, **k: computed.append(a))
    calls: list = []
    _record_probe_calls(monkeypatch, calls)

    rc, reached = _run(monkeypatch, tmp_path, source)

    assert rc == _mod._EXIT_FAIL
    assert reached == []
    assert computed == []
    assert calls == []


def test_clean_withholding_reaches_the_round_after_the_source_preflights(monkeypatch, tmp_path):
    """Order pin: published-check, then withholding, then the round body."""
    from coordinator_core.percolate import source_preflight
    from coordinator_core.percolate.source_preflight import Withholding

    source = _source_with_origin(tmp_path)
    _stamp_mirror_at(tmp_path, source)
    order: list = []
    real_check = source_preflight.check_source_published

    def check(*a, **k):
        order.append("published")
        return real_check(*a, **k)

    monkeypatch.setattr(source_preflight, "check_source_published", check)
    monkeypatch.setattr(source_preflight, "source_withholding", lambda *a, **k: order.append("withholding") or Withholding())

    rc, reached = _run(monkeypatch, tmp_path, source)

    assert rc == 0
    assert len(reached) == 1
    assert order == ["published", "withholding"]


def test_non_empty_withholding_prints_advisory_and_reaches_the_round_with_the_verdict(monkeypatch, tmp_path, capsys):
    from coordinator_core.percolate.source_preflight import Withholding

    source = _source_with_origin(tmp_path)
    _stamp_mirror_at(tmp_path, source)
    verdict = Withholding(paths={"tests/x.py": None, "y.py": "a" * 40}, walk_base="a" * 40)
    _stub_withholding(monkeypatch, verdict)
    seen = {}
    monkeypatch.setattr(_mod, "_cmd_round_default", lambda *a, **k: seen.update(k) or 0)
    (tmp_path / "root").mkdir(exist_ok=True)
    monkeypatch.setattr(_mod, "_resolve_percolate_root", lambda override: str(tmp_path / "root"))
    monkeypatch.setattr(_mod, "_branch0_gate", lambda target, root: str(source))
    monkeypatch.setattr(_mod, "_resolve_dest", lambda target, root: str(tmp_path / "dest"))
    args = _mod._build_parser().parse_args(["alpha", "--percolate-root", str(tmp_path / "root")])

    assert _mod._cmd_round(args) == 0

    assert seen["withholding"] is verdict
    assert "tests/x.py, y.py" in capsys.readouterr().out


def test_round_hands_the_withheld_json_to_publish(monkeypatch, tmp_path):
    import json

    from coordinator_core.percolate.source_preflight import Withholding

    origin, live = tmp_path / "o.git", tmp_path / "live"
    _git(["init", "-q", "--bare", str(origin)], tmp_path)
    _git(["clone", "-q", str(origin), str(live)], tmp_path)
    _git(["config", "user.email", "t@t.example"], live)
    _git(["config", "user.name", "t"], live)
    (live / "a.txt").write_text("a\n", encoding="utf-8")
    _git(["add", "--", "a.txt"], live)
    _git(["commit", "-q", "-m", "seed"], live)
    verdict = Withholding(paths={"y.py": "b" * 40, "tests/x.py": None}, walk_base="b" * 40, toplevel="/src/repo")
    captured = {}

    class _Stop(Exception):
        pass

    def run(cmd, **_kwargs):
        flag = cmd.index("--withheld-paths-json")
        captured["doc"] = json.loads(Path(cmd[flag + 1]).read_text(encoding="utf-8"))
        raise _Stop

    monkeypatch.setattr(_mod, "_run", run)
    args = _mod._build_parser().parse_args(["alpha", "--no-publish"])
    work = tmp_path / "work"
    work.mkdir()
    with pytest.raises(_Stop):
        _mod._cmd_round_default(args, "alpha", str(tmp_path), str(live), str(live), work, withholding=verdict)

    assert captured["doc"] == {"walk_base": "b" * 40, "toplevel": "/src/repo", "paths": {"y.py": "b" * 40, "tests/x.py": None}}


def test_empty_withholding_reaches_the_round_with_no_paths(monkeypatch, tmp_path):
    from coordinator_core.percolate.source_preflight import Withholding

    source = _source_with_origin(tmp_path)
    _stamp_mirror_at(tmp_path, source)
    _stub_withholding(monkeypatch, Withholding())
    seen = {}
    monkeypatch.setattr(_mod, "_cmd_round_default", lambda *a, **k: seen.update(k) or 0)
    (tmp_path / "root").mkdir(exist_ok=True)
    monkeypatch.setattr(_mod, "_resolve_percolate_root", lambda override: str(tmp_path / "root"))
    monkeypatch.setattr(_mod, "_branch0_gate", lambda target, root: str(source))
    monkeypatch.setattr(_mod, "_resolve_dest", lambda target, root: str(tmp_path / "dest"))
    args = _mod._build_parser().parse_args(["alpha", "--percolate-root", str(tmp_path / "root")])

    assert _mod._cmd_round(args) == 0
    assert not seen["withholding"].paths
