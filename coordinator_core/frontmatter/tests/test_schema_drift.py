"""The gating schema tamper-check reads a SET of schemas in constant spawns.

`check_schema_drift` used to spawn a `git show` per schema, and the revendor
script's `_verify` looped it, so verifying N schemas cost N+ processes. The read
now goes through `check_schema_drift_batch` (one `git cat-file --batch` for the whole set),
and `check_schema_drift` is its one-element call.

Pinned here: the spawn count does not grow with N, and the verdict vocabulary the
per-call form had survives -- drift is a per-entry `SchemaDriftError`, a comparison
that never ran (unusable clone, batch not completed) raises one
`SchemaProbeUnavailableError` for the whole set, and neither is ever reported for a
schema that matched.

Every test builds a throwaway DoE clone under tmp_path; nothing touches the real
vendored schemas.
"""

from __future__ import annotations

import importlib.util
import json
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from coordinator_core.frontmatter.schema_validate import (
    SchemaDriftError,
    SchemaProbeUnavailableError,
    check_schema_drift,
    check_schema_drift_batch,
)
from coordinator_core.git_scope import reset_foreign_repo_probe_memo
from coordinator_core.win_portability import no_console_creationflags

# Spawns a real external process; runs at cadence gates, not per-commit.
pytestmark = [
    pytest.mark.spawns_process,
    pytest.mark.cadence,
]

_NAMES = ("handoff.schema.json", "plan.schema.json", "lesson.schema.json")
_REVENDOR = Path(__file__).resolve().parents[3] / "bin" / "claude-klabauter-revendor-schema.py"


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
        stdin=subprocess.DEVNULL,
        **no_console_creationflags(),
    ).stdout.strip()


def _body(marker: str) -> str:
    return json.dumps({"title": marker}, indent=2) + "\n"


@pytest.fixture()
def fake_doe(tmp_path: Path) -> Path:
    if shutil.which("git") is None:
        pytest.skip("git not available")
    reset_foreign_repo_probe_memo()
    repo = tmp_path / "DoE-fake"
    (repo / "coordinator" / "schemas").mkdir(parents=True)
    for name in _NAMES:
        (repo / "coordinator" / "schemas" / name).write_text(_body(name), encoding="utf-8")
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "test@example.invalid")
    _git(repo, "config", "user.name", "schema drift test")
    _git(repo, "config", "core.autocrlf", "false")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "seed")
    return repo


@pytest.fixture()
def vendored(tmp_path: Path) -> list[Path]:
    directory = tmp_path / "vendored"
    directory.mkdir()
    paths = []
    for name in _NAMES:
        path = directory / name
        path.write_text(_body(name), encoding="utf-8")
        paths.append(path)
    return paths


@pytest.fixture()
def spawns(monkeypatch, fake_doe) -> list[list[str]]:
    # Depends on fake_doe so the repo's own setup git calls are not counted.
    seen: list[list[str]] = []
    real_run = subprocess.run

    def counting_run(argv, *args, **kwargs):  # type: ignore[no-untyped-def]
        seen.append(list(argv))
        return real_run(argv, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", counting_run)
    return seen


def _pairs(paths, ref="HEAD"):
    return [(path, ref) for path in paths]


def test_matching_set_is_green_for_every_entry(fake_doe, vendored) -> None:
    assert check_schema_drift_batch(_pairs(vendored), fake_doe) == [None] * len(vendored)


def test_a_tampered_schema_fails_alone(fake_doe, vendored) -> None:
    vendored[1].write_text(_body("tampered"), encoding="utf-8")

    results = check_schema_drift_batch(_pairs(vendored), fake_doe)

    assert results[0] is None and results[2] is None
    assert type(results[1]) is SchemaDriftError
    assert "diverges" in str(results[1])


def test_a_path_absent_at_ref_is_a_finding_not_a_could_not_check(fake_doe, vendored) -> None:
    stray = vendored[0].parent / "stray.schema.json"
    stray.write_text(_body("stray"), encoding="utf-8")

    results = check_schema_drift_batch(_pairs([vendored[0], stray]), fake_doe)

    assert results[0] is None
    assert type(results[1]) is SchemaDriftError
    assert not isinstance(results[1], SchemaProbeUnavailableError)


def test_an_unknown_ref_is_a_finding_for_every_entry(fake_doe, vendored) -> None:
    results = check_schema_drift_batch(_pairs(vendored, "no-such-ref"), fake_doe)

    assert [type(r) for r in results] == [SchemaDriftError] * len(vendored)


def test_a_pinned_ref_compares_against_the_pin_not_head(fake_doe, vendored) -> None:
    pin = _git(fake_doe, "rev-parse", "HEAD")
    (fake_doe / "coordinator" / "schemas" / _NAMES[0]).write_text(_body("moved"), encoding="utf-8")
    _git(fake_doe, "commit", "-qam", "DoE moves on")

    assert check_schema_drift_batch(_pairs(vendored, pin), fake_doe) == [None] * len(vendored)
    assert isinstance(check_schema_drift_batch(_pairs(vendored, "HEAD"), fake_doe)[0], SchemaDriftError)


def test_crlf_blob_against_lf_copy_is_not_tamper(fake_doe, vendored) -> None:
    blob = fake_doe / "coordinator" / "schemas" / _NAMES[0]
    blob.write_bytes(_body(_NAMES[0]).replace("\n", "\r\n").encode("utf-8"))
    _git(fake_doe, "commit", "-qam", "crlf")

    assert check_schema_drift_batch(_pairs([vendored[0]]), fake_doe) == [None]


def test_unusable_clone_raises_could_not_check_for_the_whole_set(tmp_path, vendored) -> None:
    with pytest.raises(SchemaProbeUnavailableError, match="NOT a drift finding"):
        check_schema_drift_batch(_pairs(vendored), tmp_path / "not-a-repo")


def test_a_batch_that_did_not_complete_raises_could_not_check(
    monkeypatch, fake_doe, vendored
) -> None:
    monkeypatch.setattr(
        "coordinator_core.frontmatter.schema_validate.scoped_cat_file_batch",
        lambda *_a, **_k: None,
    )

    with pytest.raises(SchemaProbeUnavailableError, match="NOT a drift finding"):
        check_schema_drift_batch(_pairs(vendored), fake_doe)


def test_mixed_refs_resolve_in_one_batch(spawns, fake_doe, vendored) -> None:
    pin = _git(fake_doe, "rev-parse", "HEAD")
    (fake_doe / "coordinator" / "schemas" / _NAMES[0]).write_text(_body("moved"), encoding="utf-8")
    _git(fake_doe, "commit", "-qam", "DoE moves on")
    spawns.clear()
    reset_foreign_repo_probe_memo()

    results = check_schema_drift_batch(
        [(vendored[0], pin), (vendored[1], "HEAD")], fake_doe
    )

    assert results == [None, None]
    assert len([argv for argv in spawns if "cat-file" in argv]) == 1


def test_single_form_raises_what_the_batch_returns(fake_doe, vendored) -> None:
    check_schema_drift(vendored[0], fake_doe)
    vendored[0].write_text(_body("tampered"), encoding="utf-8")

    with pytest.raises(SchemaDriftError, match="diverges"):
        check_schema_drift(vendored[0], fake_doe)


def test_empty_set_spawns_nothing(spawns, fake_doe) -> None:
    assert check_schema_drift_batch([], fake_doe) == []
    assert spawns == []


def test_spawn_count_does_not_grow_with_the_set(spawns, fake_doe, vendored) -> None:
    check_schema_drift_batch(_pairs(vendored[:1]), fake_doe)
    after_one = len(spawns)
    reset_foreign_repo_probe_memo()
    check_schema_drift_batch(_pairs(vendored), fake_doe)

    assert len(spawns) - after_one == after_one, (
        f"{len(vendored)} schemas cost {len(spawns) - after_one} spawns against "
        f"{after_one} for one: {spawns[after_one:]}"
    )


def _load_revendor():
    spec = importlib.util.spec_from_file_location("claude_klabauter_revendor_schema", _REVENDOR)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # @dataclass resolves annotations through sys.modules
    spec.loader.exec_module(module)
    return module


def test_revendor_verify_costs_one_batch_not_one_read_per_schema(
    spawns, fake_doe, vendored
) -> None:
    revendor = _load_revendor()
    pin = _git(fake_doe, "rev-parse", "HEAD")
    plans = [
        SimpleNamespace(name=path.name, vendored_path=path, pin_tracked=index == 0)
        for index, path in enumerate(vendored)
    ]
    vendored[2].write_text(_body("tampered"), encoding="utf-8")

    failures = revendor._verify(plans, fake_doe, pin)

    assert len(failures) == 1 and failures[0].startswith(f"{_NAMES[2]} (against HEAD)")
    cat_file_spawns = [argv for argv in spawns if "cat-file" in argv]
    assert len(cat_file_spawns) == 1, cat_file_spawns
    assert not [argv for argv in spawns if "show" in argv]
