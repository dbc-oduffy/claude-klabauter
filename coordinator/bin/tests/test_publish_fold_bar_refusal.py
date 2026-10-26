"""publish.py refuses a real run unless HEAD carries a green recorded full-suite verdict.

Hermetic: a tmp repo whose git dir is written by hand, so no git process runs.

Run: python -m pytest coordinator/bin/tests/test_publish_fold_bar_refusal.py -q -p no:cacheprovider
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

REAL_FOLD_BAR = True

_BIN_DIR = Path(__file__).resolve().parent.parent
_SHA = "a" * 40


def _load_publish_module():
    spec = importlib.util.spec_from_file_location("publish_fold_bar_under_test", _BIN_DIR / "publish.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


publish = _load_publish_module()


class _PastTheBar(Exception):
    """Raised by a stub right after the fold-bar check, proving the run was not refused."""


def _repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    (root / ".git" / "refs" / "heads").mkdir(parents=True)
    (root / ".git" / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    (root / ".git" / "refs" / "heads" / "main").write_text(_SHA + "\n", encoding="utf-8")
    return root


def _verdict(root: Path, **overrides) -> None:
    body = {
        "sha": _SHA,
        "tier": "full",
        "failed": 0,
        "passed": 10,
        "skipped": 0,
        "reds": [],
        "grant_ref": "grant-1",
    }
    body.update(overrides)
    path = root / "state" / "warp" / "suite-verdicts" / f"{_SHA}.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(body), encoding="utf-8")


@pytest.fixture
def run(monkeypatch):
    def _run(root: Path, argv: list) -> int:
        real = publish._fold_bar_refusal

        def _gate(repo, source_ref=""):
            assert repo == publish._REPO_ROOT
            return real(root, source_ref)

        monkeypatch.setattr(publish, "_fold_bar_refusal", _gate)

        def _stub(**_kw):
            raise _PastTheBar

        monkeypatch.setattr(publish, "_resolve_percolate_root_and_rung", _stub)
        return publish.main(argv)

    return _run


def test_missing_verdict_is_advisory_and_run_proceeds(tmp_path, run, capsys):
    with pytest.raises(_PastTheBar):
        run(_repo(tmp_path), [])
    assert "advisory, proceeding: fold bar: no recorded full-suite verdict" in capsys.readouterr().err


def test_red_verdict_is_advisory_with_failed_count_and_reds(tmp_path, run, capsys):
    root = _repo(tmp_path)
    _verdict(root, failed=1, reds=[{"nodeid": "t.py::x"}])
    with pytest.raises(_PastTheBar):
        run(root, [])
    err = capsys.readouterr().err
    assert "advisory, proceeding" in err
    assert "red without first_red_commit: t.py::x" in err
    assert "failed=1" in err and "['t.py::x']" in err


def test_green_verdict_lets_a_real_run_past_the_bar(tmp_path, run):
    root = _repo(tmp_path)
    _verdict(root)
    with pytest.raises(_PastTheBar):
        run(root, [])


def test_dry_run_is_never_gated(tmp_path, run):
    with pytest.raises(_PastTheBar):
        run(_repo(tmp_path), ["--dry-run"])


def test_round_and_publish_gates_agree_on_the_same_repo(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("percolate_round_fold_bar_parity", _BIN_DIR / "percolate-round.py")
    assert spec is not None and spec.loader is not None
    rnd = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(rnd)
    # The hand-written .git has no objects, so the round's rev-parse cannot resolve HEAD; pin it to the
    # sha publish reads from the ref file so only the verdict comparison differs between the two gates.
    monkeypatch.setattr(rnd, "_resolve_shipped_sha", lambda root, ref: _SHA)

    bare = _repo(tmp_path / "bare")
    red = _repo(tmp_path / "red")
    _verdict(red, failed=1, reds=[{"nodeid": "t.py::x"}])
    green = _repo(tmp_path / "green")
    _verdict(green)
    for root in (bare, red, green):
        assert rnd._fold_bar_refusal(root) == publish._fold_bar_refusal(root)


def test_unresolvable_source_ref_is_refused_not_gated_on_head(tmp_path):
    root = _repo(tmp_path)
    _verdict(root)
    assert publish._fold_bar_refusal(root) is None
    refusal = publish._fold_bar_refusal(root, "no-such-ref")
    assert refusal is not None and "no-such-ref" in refusal


def test_head_is_read_from_a_packed_ref(tmp_path):
    root = _repo(tmp_path)
    (root / ".git" / "refs" / "heads" / "main").unlink()
    (root / ".git" / "packed-refs").write_text(f"{_SHA} refs/heads/main\n", encoding="utf-8")
    _verdict(root)
    assert publish._fold_bar_refusal(root) is None
