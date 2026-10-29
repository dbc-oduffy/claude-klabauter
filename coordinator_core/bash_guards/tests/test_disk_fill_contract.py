"""Contract tests for _disk_fill_contract: classifier, bounded estimator, verdict."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from coordinator_core.bash_guards import _disk_fill_contract as c
from coordinator_core.bash_guards._dialect import Dialect

GiB = 1024**3
B, P = Dialect.BASH, Dialect.POWERSHELL


def _one(cmd, dialect):
    return list(c.iter_bulk_writes(cmd, dialect))


@pytest.mark.parametrize(
    "cmd,dialect,target,declared",
    [
        ("cp -r big/ dst/", B, "dst/", None),
        ("cp -R big dst", B, "dst", None),
        ("rsync -a big/ dst/", B, "dst/", None),
        ("dd if=/dev/zero of=out.img bs=1M count=100", B, "out.img", 100 * 1024**2),
        ("truncate -s 10G sparse.bin", B, "sparse.bin", 10 * GiB),
        ("Copy-Item big dst -Recurse", P, "dst", None),
        ("robocopy big dst /E", P, "dst", None),
        ("fsutil file createnew f.bin 4096", P, "f.bin", 4096),
    ],
)
def test_bulk_shapes_classify(cmd, dialect, target, declared):
    hits = _one(cmd, dialect)
    assert len(hits) == 1
    assert hits[0].target == target
    assert hits[0].declared_bytes == declared


@pytest.mark.parametrize(
    "cmd,dialect",
    [
        ("echo x > f", B),
        ("git commit -m x", B),
        ("cp a b", B),
        ("tar xf a.tar", B),
        ("Copy-Item a b", P),
        ("Set-Content f hi", P),
    ],
)
def test_non_bulk_writes_classify_to_none(cmd, dialect):
    assert _one(cmd, dialect) == []


@pytest.mark.parametrize(
    "raw,expected",
    [("10G", 10 * GiB), ("4096", 4096), ("+1M", 1024**2), ("2kb", 2000), ("x", None), ("1zz", None)],
)
def test_declared_size_parse(raw, expected):
    assert c._parse_size(raw) == expected


def test_dd_without_bs_and_count_is_unestimable():
    (hit,) = _one("dd if=/dev/zero of=out.img", B)
    assert c.estimate_output_bytes(hit) is None
    assert c.verdict("out.img", None, SimpleNamespace(volume_free_floor=10**18)).deny is False


def test_estimate_sums_source_tree(tmp_path):
    src = tmp_path / "src"
    (src / "sub").mkdir(parents=True)
    (src / "a").write_bytes(b"x" * 100)
    (src / "sub" / "b").write_bytes(b"y" * 50)
    bulk = c.BulkWrite("dst", None, ("src",), "cp -r")
    assert c.estimate_output_bytes(bulk, cwd=str(tmp_path)) == 150


def test_estimate_walk_entry_cap_returns_none(tmp_path):
    for i in range(10):
        (tmp_path / ("f%d" % i)).write_bytes(b"z")
    bulk = c.BulkWrite("dst", None, (str(tmp_path),), "cp -r")
    assert c.estimate_output_bytes(bulk, max_entries=3) is None
    assert c.estimate_output_bytes(bulk, max_entries=100) == 10


def test_estimate_walk_deadline_returns_none(tmp_path):
    (tmp_path / "f").write_bytes(b"z")
    ticks = iter(range(0, 1000))
    bulk = c.BulkWrite("dst", None, (str(tmp_path),), "cp -r")
    assert c.estimate_output_bytes(bulk, clock=lambda: float(next(ticks)), deadline_s=0.5) is None


def test_estimate_missing_source_is_zero_or_unknown(tmp_path):
    bulk = c.BulkWrite("dst", None, ("nope",), "cp -r")
    assert c.estimate_output_bytes(bulk, cwd=str(tmp_path)) in (0, None)


def _du(free):
    return lambda _p: SimpleNamespace(free=free)


def test_verdict_denies_under_floor_and_names_redirect(tmp_path):
    pol = SimpleNamespace(volume_free_floor=10 * GiB)
    v = c.verdict(str(tmp_path / "new" / "dst"), 5 * GiB, pol, _du(12 * GiB))
    assert v.deny
    assert "disk_budget sweep" in v.text
    assert "COORDINATOR_DISK_BUDGET_VOLUME_FREE_FLOOR" in v.text


def test_verdict_allows_above_floor(tmp_path):
    pol = SimpleNamespace(volume_free_floor=10 * GiB)
    assert c.verdict(str(tmp_path / "dst"), 5 * GiB, pol, _du(100 * GiB)).deny is False


def test_verdict_oserror_and_unknown_estimate_allow(tmp_path):
    pol = SimpleNamespace(volume_free_floor=10**18)

    def boom(_p):
        raise OSError("gone")

    assert c.verdict(str(tmp_path / "d"), 1, pol, boom).deny is False
    assert c.verdict(str(tmp_path / "d"), None, pol, _du(0)).deny is False


def test_target_volume_walks_to_existing_ancestor_and_uses_cwd(tmp_path):
    assert c.target_volume("a/b/c", cwd=str(tmp_path)) == tmp_path


def test_floor_env_override_honoured(tmp_path, monkeypatch):
    monkeypatch.setenv("COORDINATOR_DISK_BUDGET_VOLUME_FREE_FLOOR", "1000")
    pol = c.load_policy()
    assert pol.volume_free_floor == 1000
    dst = str(tmp_path / "dst")
    assert c.verdict(dst, 500, pol, _du(2000)).deny is False
    assert c.verdict(dst, 1500, pol, _du(2000)).deny is True


@pytest.mark.parametrize("cmd", ["truncate -s -1M f", "truncate -s 0 f", "truncate -s <1G f"])
def test_shrinking_truncate_never_denies(tmp_path, cmd):
    pol = SimpleNamespace(volume_free_floor=10 * GiB)
    for hit in _one(cmd, B):
        assert c.verdict(str(tmp_path / "f"), c.estimate_output_bytes(hit), pol, _du(0)).deny is False


def test_floor_default_without_env(monkeypatch):
    monkeypatch.delenv("COORDINATOR_DISK_BUDGET_VOLUME_FREE_FLOOR", raising=False)
    assert c.load_policy().volume_free_floor == 10 * GiB
