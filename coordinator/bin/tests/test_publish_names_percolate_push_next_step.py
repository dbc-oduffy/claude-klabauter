"""coordinator/bin/tests/test_publish_names_percolate_push_next_step.py —
regression test for a real incident (2026-08-20): a percolate round
published 9/9 rows and committed them to the mirror `claude-klabauter`, but
the mirror was never pushed — `coordinator-auto-push`
(coordinator_core/hooks/auto_push.py) declines any non-`work/*` branch by
doctrine, and a mirror publish round lands on `candidate`. The round's own
output said nothing about what to do next, so the work sat locally-committed
and invisible to the remote until a human pointed it out. There IS a
sanctioned tool for exactly this: `percolate-push <target>` — but nothing in
a successful round's output names it.

Mechanism under test: on a CLEAN round (>=1 succeeded row, no failed rows,
not `--dry-run`), `main()`'s end-of-run summary block now prints a
`Next step: ... percolate-push <target> ...` line naming the resolved
target — `mirror_expansion[0]` for a mirror round, one line per succeeded
row name otherwise. A `--dry-run` round (nothing landed) and a round with
any failed row (push-or-not is an EM judgment call on a PARTIAL sync) must
both stay silent on it.

This test drives `main()`'s REAL per-row loop — same harness shape as
`test_publish_delta_skip_row_summary_honesty.py` — with a single fake row
wired through the mirror-expansion path so `succeeded_row_names` is
populated without touching real percolate infrastructure.

Run: python -m pytest coordinator/bin/tests/test_publish_names_percolate_push_next_step.py -q
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = [
    pytest.mark.cadence,
    pytest.mark.spawns_process,
]

_BIN_DIR = Path(__file__).resolve().parent.parent
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _init_git_repo(root: Path) -> None:
    # IDEMPOTENT ON PURPOSE. This helper is called from inside the
    # monkeypatched `load_targets` fake, so it runs once per RESOLUTION, not
    # once per test. `publish.py` resolves targets twice now -- `main()` with
    # the `--target` filter, and `_declared_repo_roots_carrying_
    # coordinator_core` unfiltered -- so a second call re-seeded an already
    # committed repo and `git commit` failed "nothing to commit, working tree
    # clean". Guarding here rather than counting call sites: a fixture that
    # cannot be invoked twice encodes a production call count no test should
    # be asserting by accident.
    if (root / ".git").is_dir():
        return
    def _git(*args: str) -> None:
        subprocess.run(
            ["git", *args],
            cwd=str(root),
            capture_output=True,
            check=True,
            creationflags=_NO_WINDOW,
        )

    _git("init", "-b", "main")
    _git("config", "user.email", "publish-percolate-push-nudge-test@claude-klabauter.test")
    _git("config", "user.name", "Publish Percolate Push Nudge Test")
    _git("config", "commit.gpgsign", "false")
    keeper = root / ".gitkeep"
    keeper.write_text("", encoding="utf-8")
    _git("add", ".gitkeep")
    _git("commit", "-m", "chore: init")


def _load_publish_module():
    spec = importlib.util.spec_from_file_location(
        "publish_percolate_push_nudge_under_test", _BIN_DIR / "publish.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


publish = _load_publish_module()

_ROW_NAMES = ["row-a"]


def _wire_common_fakes(
    monkeypatch,
    tmp_path,
    *,
    fail_row: bool = False,
    rows: "list[str] | None" = None,
    sigils: "dict[str, str] | None" = None,
):
    def fake_row(name: str) -> str:
        src = tmp_path / f"src-{name}"
        dst = tmp_path / f"dst-{name}"
        src.mkdir(parents=True, exist_ok=True)
        dst.mkdir(parents=True, exist_ok=True)
        _init_git_repo(dst)
        return f"{name}|mirror|{src}|{dst}"

    monkeypatch.setattr(
        publish, "_resolve_percolate_root_and_rung", lambda **kw: (tmp_path, "test-rung")
    )
    _rows = rows if rows is not None else _ROW_NAMES
    monkeypatch.setattr(
        publish, "load_targets", lambda setup_dir, target_filter=None, **_: [
            fake_row(n) for n in _rows
        ]
    )
    # The dest-sigil map is how the next-step block groups rows by
    # DESTINATION rather than per row; default {} models plain rows that
    # share no mirror.
    monkeypatch.setattr(
        publish, "raw_dest_sigil_by_name", lambda setup_dir: dict(sigils or {})
    )

    class _FakeClaudeKlabauter:
        def resolve_target(self, store, name):
            raise KeyError(name)

        def run_parse_sweep(self, repo_root):
            return type("ParseResult", (), {"ok": True, "failures": [], "scanned": 0})()

        def enumerate_gate_entrypoints(self, repo_root):
            return ()

    monkeypatch.setattr(publish, "_import_claude_klabauter_percolate", lambda: _FakeClaudeKlabauter())
    monkeypatch.setattr(publish, "assert_percolate_store_ready", lambda engine_claude_klabauter, path: {})
    monkeypatch.setattr(publish, "locate_percolate_store", lambda setup_dir: tmp_path / "store.yaml")
    monkeypatch.setattr(publish, "resolve_percolate_identity_path", lambda setup_dir: tmp_path / "id")
    monkeypatch.setattr(publish, "check_identity_file_present", lambda path, setup_dir: tmp_path / "id")
    monkeypatch.setattr(publish, "check_identity_file_safe", lambda path: None)
    monkeypatch.setattr(
        publish,
        "parse_percolate_identity",
        lambda path: publish.PercolateIdentity(review=["dummy-pattern"]),
    )
    monkeypatch.setattr(publish, "_resolve_publish_sync_module_path", lambda setup_dir: tmp_path / "publish_sync.py")
    monkeypatch.setattr(publish, "_import_publish_sync", lambda setup_dir: object())
    monkeypatch.setattr(publish, "check_publish_sync_contract", lambda *a, **k: None)
    monkeypatch.setattr(publish, "dispatch_end_of_run_identity_check", lambda *a, **k: True)
    monkeypatch.setattr(publish, "dispatch_end_of_run_install_doc_payload_check", lambda *a, **k: True)
    monkeypatch.setattr(publish, "dispatch_end_of_run_unscanned_published_check", lambda *a, **k: True)
    monkeypatch.setattr(
        publish, "compute_delta_invalidation_signature", lambda store_path, engine_ctx: "fixed-sig"
    )
    monkeypatch.setattr(
        publish,
        "delta_row_unchanged",
        lambda setup_dir, target, signature, round_pinned_shas: False,
    )
    monkeypatch.setattr(publish, "report_candidate_divergence", lambda repo_root: None)

    def fake_process_target(target, setup_dir, totals, **kwargs):
        if fail_row:
            raise SystemExit(1)
        totals.processed += 1

    monkeypatch.setattr(publish, "process_target", fake_process_target)


def test_clean_round_names_percolate_push_with_resolved_target(monkeypatch, tmp_path, capsys):
    """A clean round (>=1 succeeded, 0 failed, not dry-run) must name the
    sanctioned next step so a committed-but-unpushed mirror is never
    silently left for a human to discover."""
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path))
    _wire_common_fakes(monkeypatch, tmp_path)

    rc = publish.main([_ROW_NAMES[0]])
    captured = capsys.readouterr()
    combined = captured.out + captured.err

    assert rc == 0
    assert "Next step: this round is committed locally, not pushed." in combined
    assert "percolate-push" in combined


def test_dry_run_does_not_print_next_step(monkeypatch, tmp_path, capsys):
    """Nothing landed under --dry-run, so the nudge must stay silent."""
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path))
    _wire_common_fakes(monkeypatch, tmp_path)

    publish.main([_ROW_NAMES[0], "--dry-run"])
    captured = capsys.readouterr()
    combined = captured.out + captured.err

    # Reachability anchor: an absence assertion is only evidence if the run
    # actually got as far as the block that would have printed. Without this,
    # an early bail for any unrelated reason satisfies both negatives below
    # while proving nothing about the condition under test.
    assert "Done." in combined
    assert "Next step:" not in combined
    assert "percolate-push" not in combined


def test_failed_row_does_not_print_next_step(monkeypatch, tmp_path, capsys):
    """A failed row makes the round PARTIAL — push-or-not is an EM judgment
    call, not a default this line should nudge."""
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path))
    _wire_common_fakes(monkeypatch, tmp_path, fail_row=True)

    publish.main([_ROW_NAMES[0]])
    captured = capsys.readouterr()
    combined = captured.out + captured.err

    # Reachability anchor (see the dry-run test above): prove the row really
    # was recorded as FAILED and the summary really rendered, so the two
    # absence assertions below cannot pass on a run that never got here.
    assert "Rows FAILED:" in combined
    assert "Next step:" not in combined
    assert "percolate-push" not in combined


def test_mirror_rows_collapse_to_one_line_naming_the_mirror_key(
    monkeypatch, tmp_path, capsys
):
    """Every row sharing a `publish-mirror:<key>` dest sigil must produce
    exactly ONE next-step line naming that mirror key.

    Regression pin for a live defect: the first cut keyed off
    `mirror_expansion`, which is set only when a single bare row name
    expands to its mirror. An ordinary no-argument publish leaves it None,
    so a real 9-row klabauter round printed NINE lines naming eight
    sub-rows nobody should invoke -- the exact noise the message register
    forbids."""
    mirror_rows = ["klab-bin", "klab-lib", "klab"]
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path))
    _wire_common_fakes(
        monkeypatch,
        tmp_path,
        rows=mirror_rows,
        sigils={n: "publish-mirror:klab-mirror" for n in mirror_rows},
    )

    rc = publish.main([])
    captured = capsys.readouterr()
    combined = captured.out + captured.err

    assert rc == 0
    next_step_lines = [ln for ln in combined.splitlines() if "Next step:" in ln]
    assert len(next_step_lines) == 1, next_step_lines
    # The token must be a REGISTERED ROW NAME, never the mirror key: mirror
    # keys are not percolate targets, and emitting one produced a live
    # MISSING_TARGET_ENTRY. Shortest-then-lexicographic picks the base row.
    assert "percolate-push klab" in next_step_lines[0]
    assert "klab-mirror" not in combined
    # The longer sibling rows are not offered as the invocation.
    for _row in ("klab-bin", "klab-lib"):
        assert f"percolate-push {_row}" not in combined


def test_non_mirror_rows_keep_their_own_lines(monkeypatch, tmp_path, capsys):
    """Grouping is by dest, not a blanket collapse: two rows that share no
    mirror sigil are two distinct destinations and each still needs its own
    push."""
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path))
    _wire_common_fakes(monkeypatch, tmp_path, rows=["solo-a", "solo-b"], sigils={})

    publish.main([])
    captured = capsys.readouterr()
    combined = captured.out + captured.err

    next_step_lines = [ln for ln in combined.splitlines() if "Next step:" in ln]
    assert len(next_step_lines) == 2, next_step_lines
    assert "percolate-push solo-a" in combined
    assert "percolate-push solo-b" in combined
