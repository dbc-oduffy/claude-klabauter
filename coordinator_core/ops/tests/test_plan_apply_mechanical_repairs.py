"""
coordinator_core.ops.tests.test_plan_apply_mechanical_repairs — the "plan.apply_mechanical_repairs" op.

Run (from repo root):
    python3 -m pytest coordinator_core/ops/tests/test_plan_apply_mechanical_repairs.py -q
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
import yaml

import coordinator_core.ops.plan_apply_mechanical_repairs as mod
from coordinator_core.authz.classification import OP_CLASSIFICATION
from coordinator_core.ops import requirement_register as rr
from coordinator_core.ops._registry_map import OP_MODULE_MAP

_SIZING_REL = "state/sizings/20260101-a.yaml"
_PLAN_REL = "docs/plans/2026-01-01-a.md"
_SIZING = (
    "schema: sizing-object\n"
    "requirement_register:\n"
    "  sources:\n"
    "  - doc: docs/x.md\n"
    "    ref: abc\n"
    "  rows:\n"
    "  - id: R1\n"
    "    source_text: first wording\n"
    "    surface: api\n"
    "    status: open\n"
    "  - id: R2\n"
    "    source_text: second wording\n"
    "    surface: api\n"
    "    status: open\n"
)
_SPINE = (
    "```yaml plan-tasks\n"
    "- id: C1\n"
    "  title: t\n"
    "  reads_at_head: [src/old/path.py]\n"
    "  writes: [src/a.py]\n"
    "```\n"
)


def _plan(claims: str, body: str = "") -> str:
    return (
        "---\ntitle: t\n"
        f"{claims}"
        "other: keep\n---\n\n# Body\n\n"
        f"{body}Reads src/old/path.py and src/old/path.py.bak.\n\n{_SPINE}"
    )


_CLAIMS_R1 = (
    "register_claims:\n"
    f"  sizing: {_SIZING_REL}\n"
    '  rows: ["R1"]\n'
    f'  pins: {{"R1": "{rr.row_pin("first wording")}"}}\n'
)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    (tmp_path / ".git").mkdir()
    (tmp_path / "state" / "sizings").mkdir(parents=True)
    (tmp_path / "docs" / "plans").mkdir(parents=True)
    (tmp_path / _SIZING_REL).write_text(_SIZING, encoding="utf-8", newline="\n")
    return tmp_path


def _seed(repo: Path, text: str, newline: str = "\n") -> Path:
    plan = repo / _PLAN_REL
    plan.write_bytes(text.replace("\n", newline).encode("utf-8"))
    return plan


def _run(repo: Path, repairs, **params) -> dict:
    params.setdefault("plan", _PLAN_REL)
    return mod._handler({**params, "repairs": repairs}, repo_root=repo / ".git")


def _fm(plan: Path) -> dict:
    return yaml.safe_load(plan.read_text(encoding="utf-8").split("---\n")[1])


def _citation(old="src/old/path.py", new="src/new/path.py") -> dict:
    return {"kind": "premise-citation-corrected", "target": old, "instruction": f"replace {old} with {new}"}


def test_registration_triple() -> None:
    assert OP_CLASSIFICATION["plan.apply_mechanical_repairs"].name == "MUTATING"
    assert OP_MODULE_MAP["plan.apply_mechanical_repairs"] == mod.__name__


def test_citation_replaced_in_body_and_spine_but_not_a_longer_path(repo):
    plan = _seed(repo, _plan(_CLAIMS_R1))
    out = _run(repo, [_citation()])
    assert out["exit_code"] == 0 and out["ok"] is True and out["skipped"] == []
    assert [a["kind"] for a in out["applied"]] == ["premise-citation-corrected"]
    text = plan.read_text(encoding="utf-8")
    assert "reads_at_head: [src/new/path.py]" in text
    assert "Reads src/new/path.py and src/old/path.py.bak." in text


def test_citation_absent_is_skipped_and_bytes_untouched(repo):
    plan = _seed(repo, _plan(_CLAIMS_R1))
    before = plan.read_bytes()
    out = _run(repo, [_citation(old="nowhere.py", new="x.py")])
    assert out["applied"] == [] and "not found" in out["skipped"][0]["reason"]
    assert plan.read_bytes() == before


def test_register_row_added_by_revise_is_claimed_and_pinned(repo):
    plan = _seed(repo, _plan(_CLAIMS_R1))
    out = _run(repo, [{"kind": "register-rows-unclaimed", "target": "REGISTER", "instruction": "claim"}])
    assert [a["kind"] for a in out["applied"]] == ["register-rows-unclaimed"]
    claims = _fm(plan)["register_claims"]
    assert claims["rows"] == ["R1", "R2"]
    register = rr.read_register(_SIZING)
    assert claims["pins"] == rr.register_pins(register, ["R1", "R2"])
    assert _fm(plan)["other"] == "keep"


def test_register_pin_missing_adds_pins_when_the_plan_has_none(repo):
    claims = f"register_claims:\n  sizing: {_SIZING_REL}\n" '  rows: ["R1", "R2"]\n'
    plan = _seed(repo, _plan(claims))
    out = _run(repo, [{"kind": "register-pin-missing", "target": "REGISTER", "instruction": "pin"}])
    assert len(out["applied"]) == 1
    register = rr.read_register(_SIZING)
    assert _fm(plan)["register_claims"]["pins"] == rr.register_pins(register, ["R1", "R2"])


def test_register_repair_on_complete_claims_and_pins_is_skipped_not_applied(repo):
    register = rr.read_register(_SIZING)
    pins = rr.register_pins(register, ["R1", "R2"])
    claims = (
        f"register_claims:\n  sizing: {_SIZING_REL}\n"
        '  rows: ["R1", "R2"]\n'
        f"  pins: {pins!r}\n"
    )
    plan = _seed(repo, _plan(claims))
    before = plan.read_bytes()
    out = _run(repo, [{"kind": "register-pin-missing", "target": "REGISTER", "instruction": "pin"}])
    assert out["applied"] == [] and "already complete" in out["skipped"][0]["reason"]
    assert plan.read_bytes() == before


def test_register_repair_refuses_a_drifted_pin_and_leaves_the_plan(repo):
    stale = _CLAIMS_R1.replace(rr.row_pin("first wording"), rr.row_pin("older wording"))
    plan = _seed(repo, _plan(stale))
    before = plan.read_bytes()
    out = _run(repo, [{"kind": "register-pin-missing", "target": "REGISTER", "instruction": "pin"}])
    assert out["applied"] == [] and "plan.repin_register" in out["skipped"][0]["reason"]
    assert plan.read_bytes() == before


def test_falsifier_row_appended_to_the_spine(repo):
    plan = _seed(repo, _plan(_CLAIMS_R1))
    repair = {"kind": "uncheckable-to-falsifier-row", "target": "A1", "instruction": "the cache is warm"}
    out = _run(repo, [repair])
    assert len(out["applied"]) == 1
    text = plan.read_text(encoding="utf-8")
    block = text.split("```yaml plan-tasks\n")[1].split("```")[0]
    rows = yaml.safe_load(block)
    assert [r["id"] for r in rows] == ["C1", "F1"]
    assert rows[1]["performer"] == "em" and rows[1]["writes"] == []
    assert "the cache is warm" in rows[1]["acceptance"]
    again = _run(repo, [repair])
    assert again["applied"] == [] and "already present" in again["skipped"][0]["reason"]


def test_unknown_kind_is_skipped_and_the_rest_still_lands(repo):
    plan = _seed(repo, _plan(_CLAIMS_R1))
    out = _run(repo, [{"kind": "mystery", "target": "x", "instruction": "y"}, _citation()])
    assert [s["kind"] for s in out["skipped"]] == ["mystery"]
    assert "unknown repair kind" in out["skipped"][0]["reason"]
    assert len(out["applied"]) == 1
    assert "src/new/path.py" in plan.read_text(encoding="utf-8")


def test_crlf_is_kept(repo):
    plan = _seed(repo, _plan(_CLAIMS_R1), newline="\r\n")
    out = _run(repo, [_citation(), {"kind": "register-rows-unclaimed", "target": "R", "instruction": "c"}])
    assert len(out["applied"]) == 2
    raw = plan.read_bytes()
    assert raw.count(b"\r\n") == raw.count(b"\n")


_NEEDLE_PATH = "scratch/harness/run_fit.py"
_ANCHOR_FM = (
    "anchor_premises:\n"
    "  - id: P3\n"
    "    claim: the harness script names the needle\n"
    '    check: {command: "grep -c -F needle scratch/harness/run_fit.py | wc -l", expect: "1"}\n'
)


def _git_repo(tmp_path: Path) -> Path:
    """A real repo whose HEAD holds the needle file; the plan written after the commit stays untracked."""
    (tmp_path / "scratch" / "harness").mkdir(parents=True)
    (tmp_path / _NEEDLE_PATH).write_text("a needle here\n", encoding="utf-8", newline="\n")
    (tmp_path / "docs" / "plans").mkdir(parents=True)
    for args in (("init", "-q"), ("add", _NEEDLE_PATH), ("commit", "-q", "-m", "base")):
        subprocess.run(
            ["git", "-C", str(tmp_path), "-c", "user.email=t@t", "-c", "user.name=t", *args],
            check=True,
            capture_output=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    return tmp_path


def _anchor_repair(command: str, **extra) -> dict:
    return {"kind": "anchor-check-corrected", "target": "P3", "command": command, **extra}


def _declared_command(plan: Path) -> str:
    return _fm(plan)["anchor_premises"][0]["check"]["command"]


def test_anchor_check_repair_that_meets_its_expect_lands_on_an_untracked_plan(tmp_path):
    repo = _git_repo(tmp_path)
    plan = _seed(repo, _plan(_ANCHOR_FM))
    command = f'git grep -c -F "needle" HEAD -- {_NEEDLE_PATH}'
    out = _run(repo, [_anchor_repair(command)])
    assert out["ok"] is True and out["skipped"] == []
    assert [a["kind"] for a in out["applied"]] == ["anchor-check-corrected"]
    assert _declared_command(plan) == command
    assert _fm(plan)["anchor_premises"][0]["check"]["expect"] == "1"
    untracked = subprocess.run(
        ["git", "-C", str(repo), "ls-files", "--others", "--exclude-standard", _PLAN_REL],
        capture_output=True, text=True, check=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    assert untracked.stdout.strip() == _PLAN_REL


def test_a_missing_path_cannot_meet_a_presence_expect_and_the_repair_is_rejected(tmp_path):
    repo = _git_repo(tmp_path)
    plan = _seed(repo, _plan(_ANCHOR_FM))
    before = plan.read_bytes()
    out = _run(repo, [_anchor_repair('git grep -c -e "" HEAD -- scratch/harness/absent_fit.py')])
    assert out["applied"] == []
    reason = out["skipped"][0]["reason"]
    assert "fails its own expect" in reason and "expected 1" in reason
    assert plan.read_bytes() == before


def test_a_missing_path_absence_claim_repair_is_written(tmp_path):
    repo = _git_repo(tmp_path)
    fm = _ANCHOR_FM.replace('expect: "1"', 'expect: "0"')
    plan = _seed(repo, _plan(fm))
    command = 'git grep -c -e "" HEAD -- scratch/harness/absent_fit.py'
    out = _run(repo, [_anchor_repair(command)])
    assert out["skipped"] == [] and [a["kind"] for a in out["applied"]] == ["anchor-check-corrected"]
    assert _declared_command(plan) == command


def test_a_ref_less_git_grep_repair_is_not_written_and_its_refusal_names_the_form(tmp_path):
    repo = _git_repo(tmp_path)
    plan = _seed(repo, _plan(_ANCHOR_FM))
    before = plan.read_bytes()
    out = _run(repo, [_anchor_repair(f"git grep -c -F needle -- {_NEEDLE_PATH}")])
    assert out["applied"] == []
    reason = out["skipped"][0]["reason"]
    assert "not evaluable in-process" in reason and "<ref> -- <path>" in reason
    assert "grep -c [-E|-F]" in reason
    assert plan.read_bytes() == before


def test_a_bad_first_repair_does_not_stick_and_a_later_valid_repair_lands(tmp_path):
    repo = _git_repo(tmp_path)
    plan = _seed(repo, _plan(_ANCHOR_FM))
    original = _declared_command(plan)
    bad = _run(repo, [_anchor_repair(f"git grep -c -F needle -- {_NEEDLE_PATH}")])
    assert bad["applied"] == [] and _declared_command(plan) == original
    plain = f"grep -c -F needle {_NEEDLE_PATH}"
    good = _run(repo, [_anchor_repair(plain)])
    assert good["skipped"] == [] and _declared_command(plan) == plain


def test_a_bad_repair_and_a_valid_repair_in_one_call_land_only_the_valid_one(tmp_path):
    repo = _git_repo(tmp_path)
    plan = _seed(repo, _plan(_ANCHOR_FM))
    good = f'git grep -c -F "needle" HEAD -- {_NEEDLE_PATH}'
    out = _run(repo, [_anchor_repair(f"git grep -c -F needle -- {_NEEDLE_PATH}"), _anchor_repair(good)])
    assert len(out["skipped"]) == 1 and len(out["applied"]) == 1
    assert _declared_command(plan) == good


def test_a_repair_with_a_wrong_count_names_the_observed_count(tmp_path):
    repo = _git_repo(tmp_path)
    plan = _seed(repo, _plan(_ANCHOR_FM))
    before = plan.read_bytes()
    out = _run(repo, [_anchor_repair(f"git grep -c -F absent_text HEAD -- {_NEEDLE_PATH}")])
    assert out["applied"] == []
    assert "observed 0, expected 1" in out["skipped"][0]["reason"]
    assert plan.read_bytes() == before


def test_a_repair_the_shell_free_runner_cannot_run_is_rejected_with_its_cause(tmp_path):
    repo = _git_repo(tmp_path)
    plan = _seed(repo, _plan(_ANCHOR_FM))
    before = plan.read_bytes()
    out = _run(repo, [_anchor_repair(f"git rev-list --count HEAD -- {_NEEDLE_PATH} | wc -l")])
    assert out["applied"] == []
    assert "unsupported shell operator '|'" in out["skipped"][0]["reason"]
    assert plan.read_bytes() == before


def test_anchor_check_repair_for_an_undeclared_anchor_is_skipped(tmp_path):
    repo = _git_repo(tmp_path)
    plan = _seed(repo, _plan(_ANCHOR_FM))
    before = plan.read_bytes()
    out = _run(repo, [_anchor_repair("grep -c -F needle x.py", target="P9")])
    assert out["applied"] == [] and "declares no anchor P9" in out["skipped"][0]["reason"]
    assert plan.read_bytes() == before


@pytest.mark.parametrize(
    "params",
    [
        {"repairs": []},
        {"plan": _PLAN_REL},
        {"plan": _PLAN_REL, "repairs": [], "extra": 1},
        {"plan": "docs/other/x.md", "repairs": []},
        {"plan": "docs/plans/absent.md", "repairs": []},
        {"plan": "../outside.md", "repairs": []},
    ],
)
def test_bad_params_refuse(repo, params):
    _seed(repo, _plan(_CLAIMS_R1))
    out = mod._handler(params, repo_root=repo / ".git")
    assert out["exit_code"] == 1 and out["ok"] is False and out["error"]
