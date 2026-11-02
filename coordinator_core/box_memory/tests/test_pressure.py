from types import SimpleNamespace

from coordinator_core.box_memory import pressure as p


def R(avail, trusted=True):
    return SimpleNamespace(avail_mb=avail, trusted=trusted)


def test_classify_both_sides():
    assert p.classify(R(1000), 2000) == p.LOW
    assert p.classify(R(2000), 2000) == p.OK
    assert p.classify(R(None), 2000) == p.UNKNOWN
    assert p.classify(R(100, trusted=False), 2000) == p.UNKNOWN


def test_storm_threshold_both_sides():
    census = [SimpleNamespace(name="grep.exe")] * 3 + ["usr/bin/find", "python.exe", "bash.exe"]
    assert p.storm_count(census) == 4
    assert p.storm(census, 4)
    assert not p.storm(census, 5)


def test_remedy_text(tmp_path):
    (tmp_path / "trim-file-cache.py").write_text("")
    t = p.remedy_text(p.KIND_LOW_MEMORY, tmp_path)
    assert t.startswith("GOVERNOR CACHE-TRIM-HINT run elevated: python ")
    assert t.endswith("trim-file-cache.py\" --apply")
    assert "unavailable" in p.remedy_text(p.KIND_LOW_MEMORY, tmp_path / "nope")
    assert "reap" in p.remedy_text(p.KIND_GREP_STORM)


def test_trim_rate_limit(tmp_path):
    stamp = tmp_path / "sub" / "last.json"
    assert p.trim_due(stamp, 120, now=1000)
    p.stamp_trim(stamp, 500, 900, now=1000)
    assert not p.trim_due(stamp, 120, now=1119)
    assert p.trim_due(stamp, 120, now=1120)
    assert p.last_trim(stamp)["after_mb"] == 900


def test_corrupt_stamp_is_due(tmp_path):
    stamp = tmp_path / "s.json"
    stamp.write_text("{not json")
    assert p.trim_due(stamp, 120, now=1)


def test_default_stamp_under_coordinator_local():
    assert ".coordinator-local" in p.default_stamp_path().parts
