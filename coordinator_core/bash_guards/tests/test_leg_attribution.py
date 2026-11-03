import time

from coordinator_core.bash_guards import leg_attribution as la


def _burn(ms: float) -> None:
    end = time.thread_time() + ms / 1000.0
    while time.thread_time() < end:
        pass


def test_inactive_is_noop_and_shared():
    assert not la.active()
    assert la.leg("a") is la.leg("b")
    with la.leg("a"):
        pass
    assert la.summary() is None


def test_active_names_legs_over_floor_sorted_and_capped():
    token = la.begin()
    try:
        with la.leg("slow"):
            _burn(80)
        with la.leg("slower"):
            _burn(120)
        with la.leg("fast"):
            pass
        s = la.summary(top=3, floor_ms=50.0)
    finally:
        la.end(token)
    assert [r["name"] for r in s] == ["slower", "slow"]
    assert all(set(r) == {"name", "ms"} for r in s)


def test_same_name_accumulates():
    token = la.begin()
    try:
        for _ in range(2):
            with la.leg("x"):
                _burn(30)
        assert la.summary(floor_ms=50.0)[0]["name"] == "x"
    finally:
        la.end(token)


def test_top_limits_rows():
    token = la.begin()
    try:
        for n in "abc":
            with la.leg(n):
                _burn(15)
        assert len(la.summary(top=2, floor_ms=10.0)) == 2
    finally:
        la.end(token)


def test_end_deactivates():
    token = la.begin()
    la.end(token)
    assert not la.active()
