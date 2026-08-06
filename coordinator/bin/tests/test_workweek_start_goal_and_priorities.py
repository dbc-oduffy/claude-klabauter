"""test_workweek_start_goal_and_priorities.py — regression suite for
workweek-start-goal-and-priorities.py, the naked-Python port of the example-doctrine-repo
coordinator/commands/workweek-start.md Steps 5/6/6.5 bash fences (M3 chunk
C-WWS).

Covers: slug/iso-week/goal_id helper formulas, the placeholder-fill +
byte-identical hash-input path (scaffold-goal, exercised directly against a
fake coordinator-doc-new stub so the test stays hermetic — no real
coordinator_core import), the YAML-aware period_value/objective extraction +
emit path (emit-goal-event, against a fake append-goal-event.py stub), and the
fail-loud branches (missing PyYAML is not exercised — venv-resident — but the
git-failure and missing-file paths for the commit subcommands are).

Spec backlink: coordinator/bin/workweek-start-goal-and-priorities.py
"""
from __future__ import annotations

import importlib.util
import json
import os
import stat
import subprocess
import sys

import pytest

_REPO_ROOT = subprocess.run(
    ["git", "rev-parse", "--show-toplevel"],
    cwd=os.path.dirname(os.path.abspath(__file__)),
    capture_output=True,
    text=True,
    check=True,
).stdout.strip()
_TARGET = os.path.join(
    _REPO_ROOT, "coordinator", "bin", "workweek-start-goal-and-priorities.py"
)


def _load_module():
    spec = importlib.util.spec_from_file_location(
        "workweek_start_goal_and_priorities", _TARGET
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def mod():
    return _load_module()


# ---------------------------------------------------------------------------
# Pure helper formulas
# ---------------------------------------------------------------------------


def test_slug_from_title_matches_bash_pipeline(mod):
    assert mod._slug_from_title("Ship the Widget: v2!!") == "ship-the-widget-v2"


def test_slug_from_title_strips_and_clamps(mod):
    long_title = "-- " + ("A" * 60) + " --"
    slug = mod._slug_from_title(long_title)
    assert not slug.startswith("-")
    assert not slug.endswith("-")
    assert len(slug) <= 40


def test_iso_week_format():
    import datetime

    mod = _load_module()
    dt = datetime.datetime(2026, 7, 13, tzinfo=datetime.timezone.utc)  # a Monday, ISO week 29
    assert mod._iso_week(dt) == "2026-W29"


def test_today_format():
    import datetime

    mod = _load_module()
    dt = datetime.datetime(2026, 7, 23, tzinfo=datetime.timezone.utc)
    assert mod._today(dt) == "2026-07-23"


def test_compute_goal_id_is_deterministic_sha1_prefix(mod):
    goal_id_1 = mod._compute_goal_id("2026-W29", "Ship the thing")
    goal_id_2 = mod._compute_goal_id("2026-W29", "Ship the thing")
    goal_id_3 = mod._compute_goal_id("2026-W29", "Ship a different thing")
    assert goal_id_1 == goal_id_2
    assert goal_id_1 != goal_id_3
    assert len(goal_id_1) == 12


# ---------------------------------------------------------------------------
# scaffold-goal — placeholder fill + byte-identical hash-input
# ---------------------------------------------------------------------------


def _make_fake_doc_new(tmp_dir, doc_new_path):
    """Write a fake coordinator-doc-new that emits the same placeholder shape
    the real _scaffold_goal() does, for --type goal --title T --out O."""
    body = '''#!/usr/bin/env python3
import json
import sys
args = sys.argv[1:]
title = args[args.index("--title") + 1]
out = args[args.index("--out") + 1]
with open(out, "w", encoding="utf-8") as fh:
    fh.write(
        "schema: goal\\n"
        "id: \\"goal-x\\"\\n"
        f"title: {json.dumps(title)}\\n"
        "status: active\\n"
        "objective: \\"PLACEHOLDER\\"\\n"
        "key_results: []\\n"
        "created: 2026-07-23\\n"
        "period: week\\n"
        "period_value: \\"PLACEHOLDER\\"\\n"
        "# weekly_perceptible: true\\n"
        "# parent_goal_id: null\\n"
        "# goal_id: \\"goal-x\\"\\n"
    )
sys.exit(0)
'''
    doc_new_path.write_text(body, encoding="utf-8")
    doc_new_path.chmod(doc_new_path.stat().st_mode | stat.S_IEXEC)


def test_scaffold_goal_fills_placeholders_with_byte_identical_hash_input(mod, tmp_path, monkeypatch):
    fake_doc_new = tmp_path / "coordinator-doc-new"
    _make_fake_doc_new(tmp_path, fake_doc_new)
    monkeypatch.setattr(mod, "_HERE", tmp_path)
    monkeypatch.setattr(mod, "_repo_slug", lambda: "local")
    monkeypatch.chdir(tmp_path)

    parser = mod._build_parser()
    out_path = tmp_path / "state" / "goals" / "2026-07-23-ship-the-thing-abcd1234.yaml"
    args = parser.parse_args(
        [
            "scaffold-goal",
            "--title",
            "Ship the thing",
            "--sid-short",
            "abcd1234",
            "--iso-week",
            "2026-W29",
            "--out",
            str(out_path),
        ]
    )
    rc = args.func(args)
    assert rc == 0
    assert out_path.exists()

    written = out_path.read_text(encoding="utf-8")
    assert 'period_value: "2026-W29"' in written
    assert "weekly_perceptible: true" in written
    assert 'objective: "Ship the thing"' in written

    expected_goal_id = mod._compute_goal_id("2026-W29", "Ship the thing")
    assert f'goal_id: "{expected_goal_id}"' in written


def test_scaffold_goal_objective_quote_escaping_is_valid_yaml(mod, tmp_path, monkeypatch):
    """Divergence-from-bash-oracle regression: a title/objective containing a
    double quote must not corrupt the emitted YAML (see _fill_goal_placeholders
    docstring)."""
    fake_doc_new = tmp_path / "coordinator-doc-new"
    _make_fake_doc_new(tmp_path, fake_doc_new)
    monkeypatch.setattr(mod, "_HERE", tmp_path)
    monkeypatch.setattr(mod, "_repo_slug", lambda: "local")
    monkeypatch.chdir(tmp_path)

    parser = mod._build_parser()
    out_path = tmp_path / "goal.yaml"
    title = 'Ship the "widget" v2'
    args = parser.parse_args(
        [
            "scaffold-goal",
            "--title",
            title,
            "--sid-short",
            "abcd1234",
            "--iso-week",
            "2026-W29",
            "--out",
            str(out_path),
        ]
    )
    rc = args.func(args)
    assert rc == 0

    mod._require_yaml()
    import yaml as _yaml

    doc = _yaml.safe_load(out_path.read_text(encoding="utf-8"))
    assert doc["objective"] == title


# ---------------------------------------------------------------------------
# emit-goal-event — YAML-aware extraction (reset + update-in-place paths)
# ---------------------------------------------------------------------------


def _make_fake_append_goal_event(path, capture_file):
    body = f'''#!/usr/bin/env python3
import json
import sys
args = sys.argv[1:]
period = args[args.index("--period") + 1]
period_value = args[args.index("--period-value") + 1]
text = args[args.index("--text") + 1]
with open({str(capture_file)!r}, "w", encoding="utf-8") as fh:
    json.dump({{"period": period, "period_value": period_value, "text": text}}, fh)
sys.exit(0)
'''
    path.write_text(body, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IEXEC)


def test_emit_goal_event_extracts_folded_block_scalar_objective(mod, tmp_path, monkeypatch):
    """Regression for Review F3 (workweek-start.md): a folded block scalar
    (`objective: >-`) must extract cleanly via YAML, which a grep+sed
    line-matcher cannot parse."""
    capture_file = tmp_path / "captured.json"
    fake_append = tmp_path / "append-goal-event.py"
    _make_fake_append_goal_event(fake_append, capture_file)
    monkeypatch.setattr(mod, "_HERE", tmp_path)

    goal_path = tmp_path / "goal.yaml"
    goal_path.write_text(
        "schema: goal\n"
        "period_value: \"2026-W29\"\n"
        "objective: >-\n"
        "  This is a folded\n"
        "  block scalar objective.\n",
        encoding="utf-8",
    )

    parser = mod._build_parser()
    args = parser.parse_args(["emit-goal-event", "--goal", str(goal_path)])
    rc = args.func(args)
    assert rc == 0

    captured = json.loads(capture_file.read_text(encoding="utf-8"))
    assert captured["period"] == "week"
    assert captured["period_value"] == "2026-W29"
    assert captured["text"] == "This is a folded block scalar objective."


# ---------------------------------------------------------------------------
# commit-priorities / commit-archive-reset — fail-loud branches
# ---------------------------------------------------------------------------


def test_commit_priorities_returns_1_when_nothing_to_stage(mod, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    parser = mod._build_parser()
    args = parser.parse_args(["commit-priorities", "--sid-short", "deadbeef"])
    rc = args.func(args)
    assert rc == 1


def test_commit_archive_reset_returns_1_when_nothing_to_stage(mod, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    parser = mod._build_parser()
    args = parser.parse_args(["commit-archive-reset", "--prior-week-start", "2026-07-13"])
    rc = args.func(args)
    assert rc == 1


# ---------------------------------------------------------------------------
# ceremony-hook — non-blocking wrapper
# ---------------------------------------------------------------------------


def test_ceremony_hook_prints_hook_output_when_nonempty(mod, tmp_path, monkeypatch):
    fake_hook = tmp_path / "coordinator-ceremony-hook.py"
    fake_hook.write_text(
        "#!/usr/bin/env python3\n"
        "print('Post-workweek-start hook: ran echo hi (exit 0)')\n",
        encoding="utf-8",
    )
    fake_hook.chmod(fake_hook.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setattr(mod, "_HERE", tmp_path)

    parser = mod._build_parser()
    args = parser.parse_args(["ceremony-hook", "--ceremony", "workweek-start"])
    rc = args.func(args)
    assert rc == 0


def test_ceremony_hook_silent_when_hook_emits_nothing(mod, tmp_path, monkeypatch, capsys):
    fake_hook = tmp_path / "coordinator-ceremony-hook.py"
    fake_hook.write_text("#!/usr/bin/env python3\n", encoding="utf-8")
    fake_hook.chmod(fake_hook.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setattr(mod, "_HERE", tmp_path)

    parser = mod._build_parser()
    args = parser.parse_args(["ceremony-hook", "--ceremony", "workweek-start"])
    rc = args.func(args)
    assert rc == 0
    out = capsys.readouterr().out
    assert out == ""
