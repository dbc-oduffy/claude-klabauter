"""The Kira Stop guard's owed-route message names only artifacts that exist."""

import json
import re
from pathlib import Path

from coordinator_core.hooks import guard_kira_verdict_routed as g
from coordinator_core.ops import review_findings_ledger as ledger

_REPO = Path(g.__file__).resolve().parents[2]


def _deny_text(tmp_path, monkeypatch) -> str:
    share = tmp_path / "subagent-share" / "s1"
    share.mkdir(parents=True)
    (share / "kira.md").write_text(
        "---\nagent_type: coordinator:overengineering-reviewer\n"
        "findings_count: 2\n---\nbody\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(g, "_share_dirs", lambda root, sid: [str(share)])
    monkeypatch.setattr(g, "_kira_repo_root", lambda payload: str(tmp_path))
    monkeypatch.setattr(g, "apply_guard_level", lambda name, d: d)
    return json.dumps(g._guard_kira_verdict_routed({"session_id": "s1", "cwd": str(tmp_path)}))


def test_owed_route_command_resolves_to_a_real_cli_and_accepts_kira(tmp_path, monkeypatch):
    text = _deny_text(tmp_path, monkeypatch)
    match = re.search(r"`(review-findings-ledger) verify --sidecar", text)
    assert match, text
    assert (_REPO / "coordinator" / "bin" / "review-findings-ledger.py").is_file()
    assert any(a.endswith("overengineering-reviewer") for a in ledger._REVIEWER_AGENT_TYPES)
    assert "verify" in ledger._build_arg_parser().format_help()
    assert "code-reviewer" not in text
