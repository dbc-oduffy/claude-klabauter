"""A re-review pass of one run re-pins the files it reviewed: the landing's verdict pin reads the run's
latest pass, so unchanged bytes land after a refire, while bytes changed after that pass still refuse."""

from __future__ import annotations

from pathlib import Path

import pytest

from coordinator_core.ops import review_prep
from coordinator_core.ops.dispatch_emit import land_run
from coordinator_core.ops.dispatch_emit.terminal_commit_review import verdict_pin_mismatches
from coordinator_core.ops.dispatch_emit.tests import test_land_run_product as prod
from coordinator_core.ops.dispatch_emit.tests.test_land_run_product import RUN, SELF, repo  # noqa: F401  (fixture)
from coordinator_core.session.machinery_paths import share_dir
from coordinator_core.session.touch_record import compute_content_hash

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_WAVE = "reviewer-sidecar.md"
_BASE_1 = "5bb42bd9392484442a8e8aa3742b66c1078ec55b"
_BASE_2 = "88160b8b3e1eac9df4462f7049d126de62dad4ac"


def _store_pass(root: Path, base: str, judged: dict, agent_type: str = "engine:prep-freeze") -> str:
    """One review.prep pass as the ask route stores it: its sidecar plus the pointers ``_write_pointers`` lays."""
    share = Path(share_dir(str(root), SELF))
    share.mkdir(parents=True, exist_ok=True)
    prefix = f"manifest-{RUN}-{base[:12]}-prep"
    sidecar = share / f"{prefix}.review-prep.md"
    pins = "".join(f"  {p}: '{sha}'\n" for p, sha in judged.items())
    sidecar.write_text(
        f"---\nagent_type: {agent_type}\nrun_base_sha: {base}\n{review_prep.VERDICT_JUDGED_KEY}:\n{pins}---\nbody\n",
        encoding="utf-8", newline="\n",
    )
    review_prep._write_pointers(share, sidecar, base, prefix, "manifest")
    return sidecar.relative_to(root).as_posix()


def _review(root: Path, prep_sidecar: str) -> dict:
    (root / _WAVE).write_text("---\nverdict: PASS\n---\nbody\n", encoding="utf-8", newline="\n")
    return {**prod.REVIEWED, "wave_sidecar_paths": [_WAVE], "prep_sidecar": prep_sidecar}


def _two_passes(root: Path) -> str:
    """Pass 1 judged older bytes of src/a.py; pass 2 re-reviewed the bytes the tree still carries.
    Returns the prep sidecar the refire's ``inline_review`` still names: pass 1's."""
    prod._author(root, "src/a.py", "a = 1\n")
    prod._declare(root, "src/a.py")
    prod._record_dir(root)
    first = _store_pass(root, _BASE_1, {"src/a.py": "0" * 64})
    _store_pass(root, _BASE_2, {"src/a.py": compute_content_hash(root / "src/a.py")})
    return first


def test_a_second_pass_over_unchanged_bytes_lands_against_the_latest_pass(repo) -> None:  # noqa: F811
    review = _review(repo, _two_passes(repo))
    assert verdict_pin_mismatches(repo, review) == []

    out = prod._land(repo, inline_review=review)

    assert out["committed"] is True, out


def test_bytes_changed_after_the_latest_pass_still_refuse(repo) -> None:  # noqa: F811
    review = _review(repo, _two_passes(repo))
    prod._author(repo, "src/a.py", "a = 2\n")

    out = prod._land(repo, inline_review=review)

    assert out["committed"] is False and out["refused"] == land_run.VERDICT_PIN_STALE, out
    assert out["paths"] == ["src/a.py"]


def test_a_single_pass_judging_other_bytes_still_refuses(repo) -> None:  # noqa: F811
    prod._author(repo, "src/a.py", "a = 1\n")
    prod._declare(repo, "src/a.py")
    prod._record_dir(repo)
    review = _review(repo, _store_pass(repo, _BASE_1, {"src/a.py": "0" * 64}))

    out = prod._land(repo, inline_review=review)

    assert out["committed"] is False and out["refused"] == land_run.VERDICT_PIN_STALE, out


def test_a_path_only_an_earlier_pass_judged_keeps_that_pin(repo) -> None:  # noqa: F811
    prod._author(repo, "src/a.py", "a = 1\n")
    prod._author(repo, "src/b.py", "b = 1\n")
    first = _store_pass(repo, _BASE_1, {"src/a.py": "0" * 64, "src/b.py": "0" * 64})
    _store_pass(repo, _BASE_2, {"src/a.py": compute_content_hash(repo / "src/a.py")})

    assert verdict_pin_mismatches(repo, _review(repo, first)) == ["src/b.py"]


def test_a_later_sidecar_not_stamped_by_the_prep_freeze_pins_nothing(repo) -> None:  # noqa: F811
    prod._author(repo, "src/a.py", "a = 1\n")
    first = _store_pass(repo, _BASE_1, {"src/a.py": "0" * 64})
    _store_pass(repo, _BASE_2, {"src/a.py": compute_content_hash(repo / "src/a.py")}, agent_type="coordinator:code-reviewer")

    assert verdict_pin_mismatches(repo, _review(repo, first)) == ["src/a.py"]
