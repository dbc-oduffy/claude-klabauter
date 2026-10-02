"""Guard: no tracked text file carries an unresolved merge-conflict hunk.

WHY THIS GUARD EXISTS
    A merge of a work branch committed 40 files with their conflict hunks
    still in them: 38 archived queue rows and two Windows `.cmd` launchers.
    Git's rename detection emits 8-character markers with a `:<path>` suffix
    (`<<<<<<<< HEAD:archive/...`), which slips past eyes trained on the
    7-character form. A YAML row with a hunk in it either fails to parse or
    parses to a mapping missing the fields one side added; a `.cmd` with a
    hunk runs both sides' REM lines and names the wrong entrypoint. Nothing
    else in the suite reads either shape as a defect, so this one does.

WHAT COUNTS
    An open (`<`) or close (`>`) marker line: 7 or 8 of the character at
    column 0, then a space. Those two are unambiguous. A bare `=======` /
    `========` line is a Markdown setext underline as often as a separator,
    so it is reported only when it sits between an open and a close marker
    in the same file -- never on its own.

    The patterns are built by repetition rather than spelled out so that
    this file cannot match its own scan.

COST
    One `git grep` spawn over the tracked tree (working-tree content, so a
    fix lands green before it is committed). Cadence tier, per the spawn
    ratchet in `test_no_new_spawning_tests.py`.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coordinator_core.win_portability import no_console_creationflags

pytestmark = [
    pytest.mark.spawns_process,
    pytest.mark.cadence,
]

REPO_ROOT = Path(__file__).resolve().parent.parent.parent

_OPEN = "^<{7,8} "
_CLOSE = "^>{7,8} "
_SEPARATOR = "^={7,8}[[:space:]]*$"


def _scan(root: Path, *, no_index: bool = False) -> dict[str, list[str]]:
    """Return {path: [offending "lineno: text" entries]} for files holding a hunk."""
    argv = ["git", "grep"]
    if no_index:
        argv.append("--no-index")
    argv += ["-n", "-I", "-E", "-e", _OPEN, "-e", _CLOSE, "-e", _SEPARATOR]
    proc = subprocess.run(
        argv,
        cwd=str(root),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        **no_console_creationflags(),
    )
    if proc.returncode > 1:
        raise AssertionError(
            f"git grep could not run the conflict-marker scan "
            f"(exit {proc.returncode}): {proc.stderr.strip()!r}"
        )

    by_file: dict[str, list[tuple[int, str]]] = {}
    for raw in proc.stdout.splitlines():
        path, lineno, text = raw.split(":", 2)
        by_file.setdefault(path, []).append((int(lineno), text.rstrip("\r")))

    offenders: dict[str, list[str]] = {}
    for path, hits in by_file.items():
        kept: list[str] = []
        inside = False
        for lineno, text in hits:
            if text.startswith("<"):
                inside = True
                kept.append(f"{lineno}: {text}")
            elif text.startswith(">"):
                inside = False
                kept.append(f"{lineno}: {text}")
            elif inside:
                kept.append(f"{lineno}: {text}")
        if any(not k.split(": ", 1)[1].startswith("=") for k in kept):
            offenders[path] = kept
    return offenders


def test_no_tracked_file_carries_a_conflict_hunk():
    offenders = _scan(REPO_ROOT)
    assert offenders == {}, (
        "Unresolved merge-conflict markers in tracked files: "
        + "; ".join(f"{p} [{', '.join(lines[:3])}]" for p, lines in sorted(offenders.items()))
        + ". Merge both sides into one valid file; for a rename conflict "
        "(8-char markers, `:<path>` suffix) take each side from the path it names."
    )


def test_scanner_sees_planted_markers_and_spares_a_setext_heading(tmp_path):
    lt, eq, gt = "<" * 8, "=" * 8, ">" * 7
    (tmp_path / "row.yaml").write_text(
        f"status: closed\n{lt} HEAD:row.yaml\nclosed_at: 2026-10-01\n{eq}\n"
        f"closed_at: 2026-09-30\n{gt} theirs\n",
        encoding="utf-8",
    )
    (tmp_path / "doc.md").write_text("Title\n=======\n\nbody\n", encoding="utf-8")

    offenders = _scan(tmp_path, no_index=True)

    assert sorted(offenders) == ["row.yaml"], offenders
    assert [entry.split(": ", 1)[0] for entry in offenders["row.yaml"]] == ["2", "4", "6"]


def test_a_scan_that_could_not_run_raises_rather_than_reading_clean(tmp_path):
    with pytest.raises(AssertionError, match="could not run the conflict-marker scan"):
        _scan(tmp_path)
