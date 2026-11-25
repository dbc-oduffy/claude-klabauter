"""A path `workflow.revise add_paths` widens into a run, and an untracked file an EM-performed plan row
declares, reach `measured-touched.json` and the clean-overlay mirror at the worktree bytes.

Each case drives the production entries: `requirements_ledger._handler` (the `workflow.revise` op),
`measured_rev.measured_rev` as the review leg calls it (`paths: _manifest.review_declared_paths`), and
`clean_overlay_verify.main --export-run` (the mirror a review leg's tests run in).
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from coordinator_core.ops.dispatch_emit import clean_overlay_verify as cov
from coordinator_core.ops.dispatch_emit import measured_rev as mr
from coordinator_core.ops.dispatch_emit import requirements_ledger as rl
from coordinator_core.ops.dispatch_emit import review_edits
from coordinator_core.ops.dispatch_emit.ask_contract import measured_rev_record_path
from coordinator_core.ops.dispatch_emit.em_only_act import row_is_em_performed
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

RUN = "ask-20261125T000000-abc123"
SIZING_REL = "state/sizings/2026-11-25-widen-overlay.yaml"
PLAN_REL = "docs/plans/2026-11-25-widen-overlay.md"
INSIDE = "pkg/inside.py"
WIDENED = "pkg/widened.py"
PEER = "pkg/peer.py"
EM_FILE = "tests/data/readback.json"
EM_IGNORED = "tests/data/ignored.json"

PLAN = f"""---
title: p
created: 2026-11-25
grouping_approvals:
  do:
    status: pending
  spun_off:
    status: pending
  defer:
    status: pending
  ruled_out:
    status: pending
---

# p

## Tasks

```yaml plan-tasks
- id: C1
  title: first
  writes:
    - {INSIDE}
- id: C6
  title: EM readback fixture
  change_kind: data-edit
  em_run: true
  writes:
    - {EM_FILE}
- id: C7
  title: EM ignored scratch fixture
  change_kind: data-edit
  em_run: true
  writes:
    - {EM_IGNORED}
```
"""


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(root), "-c", "user.name=t", "-c", "user.email=t@t", "-c", "core.autocrlf=false", *args],
        check=True, capture_output=True, text=True, **no_console_creationflags(),
    ).stdout.strip()


@pytest.fixture(autouse=True)
def _isolated_system_temp(tmp_path, monkeypatch):
    import tempfile

    (tmp_path / "systemp").mkdir()
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path / "systemp"))


@pytest.fixture
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A committed repo, a staged run (record dir, `manifest.json` frozen at the run's first staging, plan
    with a spine) whose plan has an EM-performed row; the EM's fixture is written untracked."""
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    for rel in (INSIDE, WIDENED, PEER):
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text("V = 1\n", encoding="utf-8", newline="\n")
    (root / ".gitignore").write_text(f"{EM_IGNORED}\n", encoding="utf-8", newline="\n")
    (root / "docs" / "plans").mkdir(parents=True)
    (root / PLAN_REL).write_text(PLAN, encoding="utf-8", newline="\n")
    (root / "state" / "sizings").mkdir(parents=True)
    sizing = {
        "schema": "sizing-object", "intent": "x", "estimate": {"tshirt": "S", "provisional": True},
        "route": "dispatch", "detents": [], "fork": None, "xl_exit": None, "status": "sized",
        "premise": {"provenance": "not-applicable", "evidence": "fixture"},
        "pm_verbatims": [{"turn": 1, "text": "do x"}], "plan": PLAN_REL,
    }
    (root / SIZING_REL).write_text(yaml.safe_dump(sizing, sort_keys=False), encoding="utf-8", newline="\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "base")

    rl.start(root, RUN, all_of_pm_ask=False)
    rl.bind(root, RUN, SIZING_REL, ask_text="a raw ask")
    record = measured_rev_record_path(root, RUN).parent
    assert rl.run_dir(root, RUN) == record, "fixture: the run's record dir is not the measured-rev record dir"
    record.mkdir(parents=True, exist_ok=True)
    manifest = {
        "plan_rel": PLAN_REL,
        "review_declared_paths": [INSIDE],
        "rows": [{"id": "C1", "writes": [INSIDE], "writes_under": []}],
    }
    (record / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8", newline="\n")

    (root / INSIDE).write_text("V = 2\n", encoding="utf-8", newline="\n")
    (root / PEER).write_text("V = 99\n", encoding="utf-8", newline="\n")
    monkeypatch.setattr(cov, "_own_written", lambda *_a, **_k: [])
    monkeypatch.setattr(cov, "_snapshot_scope", lambda *_a, **_k: [])
    monkeypatch.setattr(review_edits, "live_sibling_declared", lambda *_a, **_k: {})
    return root


def _review_leg_measure(root: Path) -> dict:
    """The review leg's call: the manifest's `review_declared_paths` as `paths`, nothing else."""
    manifest = json.loads((measured_rev_record_path(root, RUN).parent / "manifest.json").read_text(encoding="utf-8"))
    return mr.measured_rev(RUN, manifest["review_declared_paths"], repo_root=root)


def _mirror_holds(root: Path, rel: str, expected: bytes, capsys) -> tuple[int, str]:
    """Exit status and output of one probe run in the clean export `--export-run` builds; the probe names
    `rel` as MISSING or STALE when the mirror lacks it or holds other bytes."""
    probe = (
        "import pathlib, sys\n"
        "p = pathlib.Path(sys.argv[1])\n"
        "if not p.is_file():\n    print('MISSING ' + sys.argv[1]); sys.exit(3)\n"
        "if p.read_bytes() != sys.argv[2].encode():\n    print('STALE ' + sys.argv[1]); sys.exit(4)\n"
    )
    capsys.readouterr()
    code = cov.main(["--export-run", "--run-id", RUN, "--repo-root", str(root), "--",
                     sys.executable, "-c", probe, rel, expected.decode()])
    seen = capsys.readouterr()
    return code, seen.out + seen.err


def _widen(root: Path, path: str) -> None:
    reply = rl._handler({"run_id": RUN, "add_paths": [path]}, repo_root=root)
    assert reply.get("ok") is True, reply
    assert [w["paths"] for w in reply["footprint_widened"]] == [[path]], reply
    assert path in rl.run_footprint(root, rl.run_dir(root, RUN), SIZING_REL)[0]


def test_a_path_revise_widens_after_a_measurement_is_in_the_next_review_measurement(world: Path) -> None:
    _review_leg_measure(world)
    assert WIDENED not in mr.touched_paths(world, RUN)
    _widen(world, WIDENED)
    (world / WIDENED).write_text("V = 2\n", encoding="utf-8", newline="\n")

    after = _review_leg_measure(world)

    assert WIDENED in mr.touched_paths(world, RUN), mr.touched_paths(world, RUN)
    assert WIDENED in after["paths"]


def test_a_path_revise_widens_after_a_measurement_reaches_the_mirror_at_its_worktree_bytes(world: Path, capsys) -> None:
    _review_leg_measure(world)
    _widen(world, WIDENED)
    (world / WIDENED).write_text("V = 2\n", encoding="utf-8", newline="\n")
    _review_leg_measure(world)

    code, out = _mirror_holds(world, WIDENED, b"V = 2\n", capsys)

    assert code == 0, out


def test_an_undeclared_dirty_file_stays_out_of_the_measurement_and_the_mirror(world: Path, capsys) -> None:
    _review_leg_measure(world)
    _widen(world, WIDENED)
    (world / WIDENED).write_text("V = 2\n", encoding="utf-8", newline="\n")
    _review_leg_measure(world)

    assert PEER not in mr.touched_paths(world, RUN)
    code, out = _mirror_holds(world, PEER, b"V = 1\n", capsys)
    assert code == 0, out


def test_the_em_row_fixture_shape_is_em_performed() -> None:
    row = {"id": "C6", "title": "t", "change_kind": "data-edit", "em_run": True, "writes": [EM_FILE]}

    assert row_is_em_performed(row) is True
    assert row_is_em_performed({**row, "writes": [INSIDE]}) is False


def test_an_untracked_file_an_em_row_declares_is_in_the_measurement(world: Path) -> None:
    (world / EM_FILE).parent.mkdir(parents=True, exist_ok=True)
    (world / EM_FILE).write_text('{"ue": 1}\n', encoding="utf-8", newline="\n")

    result = _review_leg_measure(world)

    assert EM_FILE in mr.touched_paths(world, RUN), mr.touched_paths(world, RUN)
    assert EM_FILE in result["paths"]


def test_an_untracked_file_an_em_row_declares_reaches_the_mirror_at_its_worktree_bytes(world: Path, capsys) -> None:
    (world / EM_FILE).parent.mkdir(parents=True, exist_ok=True)
    (world / EM_FILE).write_text('{"ue": 1}\n', encoding="utf-8", newline="\n")
    _review_leg_measure(world)

    code, out = _mirror_holds(world, EM_FILE, b'{"ue": 1}\n', capsys)

    assert code == 0, out


def test_a_gitignored_untracked_file_an_em_row_declares_stays_out(world: Path, capsys) -> None:
    (world / EM_FILE).parent.mkdir(parents=True, exist_ok=True)
    (world / EM_FILE).write_text('{"ue": 1}\n', encoding="utf-8", newline="\n")
    (world / EM_IGNORED).write_text("scratch\n", encoding="utf-8", newline="\n")

    _review_leg_measure(world)

    assert EM_IGNORED not in mr.touched_paths(world, RUN)
    code, out = _mirror_holds(world, EM_IGNORED, b"scratch\n", capsys)
    assert code == 3 and "MISSING" in out, out
