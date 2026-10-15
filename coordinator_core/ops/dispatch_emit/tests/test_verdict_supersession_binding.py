"""latest_delivery_binding: the newest superseding record's path and diff_sha."""

from __future__ import annotations

from pathlib import Path

from coordinator_core.ops.dispatch_emit import verdict_supersession as vs

RUN = "state/review-runs/plan.md"


def _record(root: Path, name: str, recorded_at: str, **extra: str) -> None:
    d = root / vs.VERDICT_DIR / "plan"
    d.mkdir(parents=True, exist_ok=True)
    lines = [
        "---",
        f"kind: {vs.RECORD_KIND}",
        f"supersedes: {RUN}",
        f"recorded_at: '{recorded_at}'",
        "delivery: {verdict: PASS}",
        *[f"{k}: {v}" for k, v in extra.items()],
        "---",
        "",
    ]
    (d / name).write_text("\n".join(lines), encoding="utf-8")


def test_newest_record_wins_with_path_and_diff_sha(tmp_path):
    _record(tmp_path, "a.md", "2026-10-15T10:00:00+00:00", diff_sha="aaa")
    _record(tmp_path, "b.md", "2026-10-15T11:00:00+00:00", diff_sha="bbb", head_sha="hhh")
    got = vs.latest_delivery_binding(tmp_path, RUN)
    assert got == {
        "record_path": "state/delivery-verdicts/plan/b.md",
        "diff_sha": "bbb",
    }
    assert vs._newest_supersession(tmp_path, RUN)["diff_sha"] == "bbb"


def test_record_without_diff_sha_returns_none_diff(tmp_path):
    _record(tmp_path, "a.md", "2026-10-15T10:00:00+00:00")
    got = vs.latest_delivery_binding(tmp_path, RUN)
    assert got["diff_sha"] is None


def test_no_record_returns_none(tmp_path):
    assert vs.latest_delivery_binding(tmp_path, RUN) is None

