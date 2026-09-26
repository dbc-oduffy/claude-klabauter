"""test_coordinator_doc_new_baton_sizing_object.py — C8 coverage: `--sizing-object`
on the `handoff` and `spinoff` scaffold arms of `coordinator-doc-new`, and the
handoff arm's carry into C1's sizing rung.

docs/plans/2026-09-26-batons-carry-their-work-c7-c11.md § C8. Before this
chunk, `--sizing-object` was silently IGNORED on `handoff`/`spinoff` — parsed
by argparse, never emitted, never fed to the deliverable-id carry. This suite
pins the fix:

1. A handoff scaffolded with the flag (and `--new-chain`, so no ambient
   plan/predecessor rung competes) emits `sizing_object:` and carries the
   cited sizing's `deliverable_id` verbatim (C1's rung, fed only by this
   arm's explicit citation).
2. A spinoff scaffolded with the flag emits the key too, but mints its OWN
   id regardless — a spinoff is the sole bearer of its own id (PM ruling
   2026-08-05) and the spinoff arm never feeds the carry.
3. An unresolvable `--sizing-object` on `handoff` is refused at write time
   (exit 1, no file) — the same bar the `plan`/`roadmap-baton` arms already
   hold, now widened to `handoff`/`spinoff`.
4. The cited sizing's own bytes (`plan:`/`status:`) are untouched by either
   scaffold — the reverse-edge gate stays `plan`-only (§ Design decisions).
5. `--sizing-object` is refused outright on a type that accepts neither the
   requirement nor the optional citation (`recovery`).
6. A handoff citing a sizing that DISAGREES with an explicit `--predecessor`
   carrying a different id hits the arm's PRE-EXISTING divergence posture
   unchanged: the cascade raises `DivergentDeliverableIdError` naming the
   sizing path (C1), and the handoff arm's own narrow catch degrades that to
   a fresh mint-from-slug plus a stderr degradation note rather than a hard
   refusal — the file is still written, with neither disagreeing id.

Negative-spec: does NOT re-cover the plan/roadmap-baton explicit-answer gate
(`--sizing-object`/`--no-sizing-object` mutual exclusion, the REQUIRED-answer
refusal) — that is
test_coordinator_doc_new_sizing_object_gate.py's and
test_coordinator_doc_new_roadmap_baton_sizing_gate.py's surface, both
unaffected by this chunk (§ C8's requirement half stays plan/roadmap-baton
only). Does NOT re-cover the plan arm's reverse-edge writer — that is
test_coordinator_doc_new_sizing_reverse_edge.py's surface, likewise unaffected
(handoff/spinoff never write it).

In-process by construction — `main()` is driven through `sys.argv` rather
than spawned, same idiom as test_coordinator_doc_new_roadmap_baton_sizing_gate.py,
so this file adds nothing to the spawn ratchet.

Run:
    pytest coordinator/bin/tests/test_coordinator_doc_new_baton_sizing_object.py -v
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[3]
_CLI_PATH = _REPO_ROOT / "coordinator" / "bin" / "coordinator-doc-new.py"

#: Minimal whole-document sizing YAML — `_read_sizing_meta` is a bare
#: `yaml.safe_load` with no schema check on the READ side (only the plan
#: arm's reverse-edge WRITE validates against the schema), so a mapping
#: carrying `deliverable_id`/`status`/`plan` is sufficient here.
_SIZING_YAML = (
    "deliverable_id: \"dlv-cited-sizing-aaaaaa\"\n"
    "status: sized\n"
    "plan: null\n"
)


def _load_cli():
    """The scaffolder as a module. Loaded by path because the file's name is
    hyphenated and it is a CLI, not an importable package member."""
    spec = importlib.util.spec_from_file_location("coordinator_doc_new_under_test", _CLI_PATH)
    assert spec is not None and spec.loader is not None, f"could not load {_CLI_PATH}"
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def cli():
    return _load_cli()


def _run(cli, monkeypatch, tmp_path: Path, *args: str) -> int:
    """`main()` under a supplied argv, returning its exit code."""
    monkeypatch.setattr(sys, "argv", ["coordinator-doc-new", *args])
    monkeypatch.chdir(tmp_path)
    try:
        return int(cli.main() or 0)
    except SystemExit as exc:
        return int(exc.code or 0)


def _write_sizing(tmp_path: Path, rel: str = "state/sizings/2026-09-26-cited.yaml") -> str:
    sizing_path = tmp_path / rel
    sizing_path.parent.mkdir(parents=True, exist_ok=True)
    sizing_path.write_text(_SIZING_YAML, encoding="utf-8")
    return rel


def test_handoff_with_sizing_object_carries_the_sizing_id(cli, monkeypatch, tmp_path):
    sizing_rel = _write_sizing(tmp_path)
    out_path = tmp_path / "handoff.md"

    code = _run(
        cli, monkeypatch, tmp_path,
        "--type", "handoff",
        "--title", "A baton sized against a cited sizing",
        "--new-chain",
        "--sizing-object", sizing_rel,
        "--out", str(out_path),
    )

    assert code == 0
    body = out_path.read_text(encoding="utf-8")
    assert f'sizing_object: "{sizing_rel}"' in body
    assert 'deliverable_id: "dlv-cited-sizing-aaaaaa"' in body


def test_spinoff_with_sizing_object_emits_key_but_mints_its_own_id(cli, monkeypatch, tmp_path):
    sizing_rel = _write_sizing(tmp_path)
    out_path = tmp_path / "spinoff.md"

    code = _run(
        cli, monkeypatch, tmp_path,
        "--type", "spinoff",
        "--title", "A spinoff citing a sizing for provenance only",
        "--sizing-object", sizing_rel,
        "--out", str(out_path),
    )

    assert code == 0, out_path.read_text(encoding="utf-8") if out_path.exists() else ""
    body = out_path.read_text(encoding="utf-8")
    assert f'sizing_object: "{sizing_rel}"' in body
    # Sole-bearer invariant: a spinoff never carries the cited sizing's id.
    assert 'deliverable_id: "dlv-cited-sizing-aaaaaa"' not in body


def test_handoff_with_unresolvable_sizing_object_refuses_and_writes_nothing(cli, monkeypatch, tmp_path):
    out_path = tmp_path / "handoff.md"

    code = _run(
        cli, monkeypatch, tmp_path,
        "--type", "handoff",
        "--title", "A baton citing a sizing that does not exist",
        "--new-chain",
        "--sizing-object", "state/sizings/absent.yaml",
        "--out", str(out_path),
    )

    assert code == 1
    assert not out_path.exists()


def test_handoff_scaffold_leaves_cited_sizing_bytes_unchanged(cli, monkeypatch, tmp_path):
    sizing_rel = _write_sizing(tmp_path)
    sizing_path = tmp_path / sizing_rel
    before = sizing_path.read_text(encoding="utf-8")
    out_path = tmp_path / "handoff.md"

    code = _run(
        cli, monkeypatch, tmp_path,
        "--type", "handoff",
        "--title", "A baton that must not mutate the sizing it cites",
        "--new-chain",
        "--sizing-object", sizing_rel,
        "--out", str(out_path),
    )

    assert code == 0
    assert sizing_path.read_text(encoding="utf-8") == before


def test_spinoff_scaffold_leaves_cited_sizing_bytes_unchanged(cli, monkeypatch, tmp_path):
    sizing_rel = _write_sizing(tmp_path)
    sizing_path = tmp_path / sizing_rel
    before = sizing_path.read_text(encoding="utf-8")
    out_path = tmp_path / "spinoff.md"

    code = _run(
        cli, monkeypatch, tmp_path,
        "--type", "spinoff",
        "--title", "A spinoff that must not mutate the sizing it cites",
        "--sizing-object", sizing_rel,
        "--out", str(out_path),
    )

    assert code == 0
    assert sizing_path.read_text(encoding="utf-8") == before


def test_sizing_object_refused_on_a_type_that_does_not_accept_it(cli, monkeypatch, tmp_path):
    sizing_rel = _write_sizing(tmp_path)
    out_path = tmp_path / "recovery.md"

    code = _run(
        cli, monkeypatch, tmp_path,
        "--type", "recovery",
        "--title", "A recovery baton, which has no sizing citation surface",
        "--sizing-object", sizing_rel,
        "--out", str(out_path),
    )

    assert code == 1
    assert not out_path.exists()


def test_handoff_with_sizing_disagreeing_with_predecessor_degrades_and_names_the_sizing_path(
    cli, monkeypatch, tmp_path, capsys,
):
    """The handoff arm's EXISTING divergence posture (narrow catch around
    `DroppedDeliverableJoinError`/`DivergentDeliverableIdError`, unchanged by
    C1/C2) degrades to mint-from-slug and writes a degradation note rather
    than refusing the whole scaffold — the loud REFUSAL lives inside the
    cascade's own divergence message, which C1 pins names the sizing path.
    This is the sizing rung's disagreement case reaching that existing
    posture end to end, not a new hard-refusal path."""
    sizing_rel = _write_sizing(tmp_path)

    predecessor_path = tmp_path / "predecessor.md"
    predecessor_path.write_text(
        "---\n"
        "deliverable_id: \"dlv-predecessor-side-bbbbbb\"\n"
        "---\n\n# predecessor\n",
        encoding="utf-8",
    )
    out_path = tmp_path / "handoff.md"

    code = _run(
        cli, monkeypatch, tmp_path,
        "--type", "handoff",
        "--title", "A baton whose sizing and predecessor disagree",
        "--predecessor", str(predecessor_path),
        "--sizing-object", sizing_rel,
        "--out", str(out_path),
    )

    assert code == 0
    assert out_path.exists()
    err = capsys.readouterr().err
    assert "DivergentDeliverableIdError" in err
    assert sizing_rel in err
    assert "dlv-cited-sizing-aaaaaa" in err
    assert "dlv-predecessor-side-bbbbbb" in err
    # Neither disagreeing id is silently picked as a winner — the cascade
    # degrades to a fresh mint-from-slug instead.
    body = out_path.read_text(encoding="utf-8")
    assert "dlv-cited-sizing-aaaaaa" not in body
    assert "dlv-predecessor-side-bbbbbb" not in body


def test_handoff_without_the_flag_is_unaffected(cli, monkeypatch, tmp_path):
    """Negative control — widening the arm to accept `--sizing-object` must
    not change the byte-identical output of a handoff that never passes it."""
    out_path = tmp_path / "handoff.md"

    code = _run(
        cli, monkeypatch, tmp_path,
        "--type", "handoff",
        "--title", "An ordinary handoff with no sizing citation",
        "--new-chain",
        "--out", str(out_path),
    )

    assert code == 0
    assert "sizing_object:" not in out_path.read_text(encoding="utf-8")


def test_no_sizing_object_is_refused_on_handoff(cli, monkeypatch, tmp_path):
    """`--no-sizing-object` only ever declares absence against the plan/
    roadmap-baton explicit-answer gate — handoff/spinoff have no such gate
    to satisfy, so the flag is refused there rather than silently accepted."""
    out_path = tmp_path / "handoff.md"

    code = _run(
        cli, monkeypatch, tmp_path,
        "--type", "handoff",
        "--title", "A baton wrongly declaring sizing absence",
        "--new-chain",
        "--no-sizing-object",
        "--out", str(out_path),
    )

    assert code == 1
    assert not out_path.exists()
