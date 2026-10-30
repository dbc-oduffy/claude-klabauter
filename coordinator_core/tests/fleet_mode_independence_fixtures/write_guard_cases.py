"""Deny/allow fixtures for denial-class `coordinator_core.write_guards` modules.

Keyed by dotted module name; the contract is in `__init__.py`.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

import pytest

from coordinator_core.tests.fleet_mode_independence_fixtures import Case, GuardFixtures

_WG = "coordinator_core.write_guards."
_AGENT = "a1b2c3d4e5f6a7b8"
_SESSION = "sess-4f9a2c1d"

FIXTURES: dict[str, GuardFixtures] = {}


def _repo(root: Path) -> Path:
    (root / ".git").mkdir(parents=True, exist_ok=True)
    return root


def _put(path: Path, text: str = "x\n") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _payload(tool: str, path: Any, cwd: Path | None = None, **tool_input: Any) -> dict:
    key = "notebook_path" if tool == "NotebookEdit" else "file_path"
    payload: dict = {"tool_name": tool, "tool_input": {key: str(path), **tool_input}}
    if cwd is not None:
        payload["cwd"] = str(cwd)
    return payload


def _agent(payload: dict, agent_id: str = _AGENT, session_id: str = _SESSION) -> dict:
    return {**payload, "agent_id": agent_id, "session_id": session_id}


def _dispatch_row(root: Path, subagent_type: str, agent_id: str = _AGENT, session_id: str = _SESSION) -> None:
    sessions = root / ".git" / "coordinator-sessions"
    _put(sessions / ".agents" / agent_id / "em-session-id.txt", session_id + "\n")
    _put(sessions / session_id / "dispatched-agents.txt", f"{agent_id}\tx\t{subagent_type}\n")


def _env(mp: pytest.MonkeyPatch, **values: str) -> None:
    for key, value in values.items():
        mp.setenv(key, value)


def _clear(mp: pytest.MonkeyPatch, *names: str) -> None:
    for name in names:
        mp.delenv(name, raising=False)


def _case(build: Callable[[Path, pytest.MonkeyPatch], dict], *clear: str) -> Case:
    def case(root: Path, mp: pytest.MonkeyPatch) -> dict:
        _clear(mp, *clear)
        return build(root, mp)

    return case


def _fixtures(deny: list[Case], allow: list[Case]) -> GuardFixtures:
    return {"deny": deny, "allow": allow}


# --- block_completion_monolith_write ---
_OV_MONOLITH = "COORDINATOR_OVERRIDE_COMPLETION_MONOLITH"
FIXTURES[_WG + "block_completion_monolith_write"] = _fixtures(
    deny=[_case(lambda r, mp: _payload("Write", r / "archive/completed/2026-10.md", r, content="x"), _OV_MONOLITH)],
    allow=[_case(lambda r, mp: _payload("Write", r / "archive/completed/legacy/2026-10.md", r, content="x"), _OV_MONOLITH)],
)

# --- block_goals_log_hand_write ---
_OV_GOALS = "COORDINATOR_OVERRIDE_GOALS_LOG_WRITE"
FIXTURES[_WG + "block_goals_log_hand_write"] = _fixtures(
    deny=[_case(lambda r, mp: _payload("Write", r / "state/goals-log.main.jsonl", r, content="x"), _OV_GOALS)],
    allow=[_case(lambda r, mp: _payload("Write", r / "state/goals.md", r, content="x"), _OV_GOALS)],
)

# --- block_hand_authored_handoff_creation ---
_OV_HANDOFF = "COORDINATOR_OVERRIDE_HAND_HANDOFF_WRITE"
FIXTURES[_WG + "block_hand_authored_handoff_creation"] = _fixtures(
    deny=[_case(lambda r, mp: _payload("Write", r / "state/handoffs/new.md", r, content="x"), _OV_HANDOFF)],
    allow=[
        _case(
            lambda r, mp: _payload("Write", _put(r / "state/handoffs/old.md"), r, content="y"),
            _OV_HANDOFF,
        )
    ],
)

# --- block_fleet_delegation_write ---
def _fleet_delegation(r: Path, mp: pytest.MonkeyPatch, name: str) -> dict:
    from coordinator_core._settings_home import settings_home

    return _payload("Write", settings_home() / name, r, content="{}")


FIXTURES[_WG + "block_fleet_delegation_write"] = _fixtures(
    deny=[lambda r, mp: _fleet_delegation(r, mp, "fleet-delegation.json")],
    allow=[lambda r, mp: _fleet_delegation(r, mp, "other.json")],
)

# --- block_hand_set_test_verdict ---
_VERDICT_LEAF = "state/subagent-share/{sid}/{leaf}"
FIXTURES[_WG + "block_hand_set_test_verdict"] = _fixtures(
    deny=[
        lambda r, mp: _payload(
            "Write", r / _VERDICT_LEAF.format(sid=_SESSION, leaf="test-runner.aaaa1111bbbb2222.md"), r,
            content="---\nstatus: pass\n---\n",
        )
    ],
    allow=[
        lambda r, mp: _payload(
            "Write", r / _VERDICT_LEAF.format(sid=_SESSION, leaf="test-runner.aaaa1111bbbb2222.md"), r,
            content="no verdict line\n",
        )
    ],
)

# --- block_duplicate_decision_record_id ---
def _decision_dir(r: Path) -> Path:
    _put(r / "docs/decisions/DR-001-a.md", "---\nid: DR-001\n---\nbody\n")
    return r / "docs/decisions"


FIXTURES[_WG + "block_duplicate_decision_record_id"] = _fixtures(
    deny=[
        lambda r, mp: _payload(
            "Write", _decision_dir(r) / "DR-002-b.md", r, content="---\nid: DR-001\n---\nbody\n"
        )
    ],
    allow=[
        lambda r, mp: _payload(
            "Write", _decision_dir(r) / "DR-002-b.md", r, content="---\nid: DR-002\n---\nbody\n"
        )
    ],
)

# --- sentinel-write guards: the sentinel is denied, a sibling file is not ---
for _module, _sentinel, _override in (
    ("block_dev_repo_sentinel_write", ".coordinator-dev-repo", "COORDINATOR_OVERRIDE_DEV_REPO_SENTINEL"),
    ("block_worktree_sentinel_write", ".coordinator-override-worktree-guard", "COORDINATOR_OVERRIDE_WORKTREE_GUARD"),
):
    FIXTURES[_WG + _module] = _fixtures(
        deny=[_case(lambda r, mp, s=_sentinel: _payload("Write", r / s, r, content="x"), _override)],
        allow=[_case(lambda r, mp: _payload("Write", r / "notes.md", r, content="x"), _override)],
    )


def _disarm_marker() -> str:
    from coordinator_core.bash_guards._blanket_disarm import MARKER_BASENAME

    return MARKER_BASENAME


FIXTURES[_WG + "block_disarm_marker_sentinel_write"] = _fixtures(
    deny=[lambda r, mp: _payload("Write", r / _disarm_marker(), r, content="x")],
    allow=[lambda r, mp: _payload("Write", r / "notes.md", r, content="x")],
)

# --- block_cross_repo_agent_write ---
_SESSION_UUID = "11111111-2222-3333-4444-555555555555"


def _cross_repo(r: Path, mp: pytest.MonkeyPatch, inside: bool) -> dict:
    from coordinator_core import machine_resolver

    run, sibling = _repo(r / "run-repo"), _repo(r / "sib-repo")
    mp.setattr(
        machine_resolver,
        "merged_flat_registry",
        lambda: {"repos.run": str(run), "repos.sib": str(sibling)},
    )
    target = (run if inside else sibling) / "src" / "a.py"
    return {
        "tool_name": "Edit",
        "tool_input": {"file_path": str(target)},
        "agent_id": "agent-1",
        "session_id": _SESSION_UUID,
        "cwd": str(run),
    }


FIXTURES[_WG + "block_cross_repo_agent_write"] = _fixtures(
    deny=[lambda r, mp: _cross_repo(r, mp, False)],
    allow=[lambda r, mp: _cross_repo(r, mp, True)],
)

# --- block_confined_agent_write ---
def _confined(r: Path, mp: pytest.MonkeyPatch, rel: str) -> dict:
    _repo(r)
    _dispatch_row(r, "coordinator:code-reviewer")
    return _agent(_payload("Write", r / rel, r, content="x"))


_OV_CONFINED = "COORDINATOR_OVERRIDE_CONFINED_AGENT_WRITE"
FIXTURES[_WG + "block_confined_agent_write"] = _fixtures(
    deny=[_case(lambda r, mp: _confined(r, mp, "src/a.py"), _OV_CONFINED)],
    allow=[_case(lambda r, mp: _confined(r, mp, f".coordinator-local/subagent-share/{_SESSION}/a.md"), _OV_CONFINED)],
)

# --- block_foreign_family_sidecar_write ---
def _foreign_family(r: Path, mp: pytest.MonkeyPatch, leaf_agent: str) -> dict:
    _repo(r)
    _dispatch_row(r, "coordinator:executor")
    leaf = f"state/subagent-share/{_SESSION}/coordinator-executor.{leaf_agent}.md"
    return _agent(_payload("Write", r / leaf, r, content="x"))


FIXTURES[_WG + "block_foreign_family_sidecar_write"] = _fixtures(
    deny=[lambda r, mp: _foreign_family(r, mp, "bbbbbbbbbbbbbbbb")],
    allow=[lambda r, mp: _foreign_family(r, mp, _AGENT)],
)

# --- block_hand_authored_sidecar_creation ---
_OV_SIDECAR = "COORDINATOR_OVERRIDE_HAND_SIDECAR_WRITE"


def _hand_sidecar(r: Path, mp: pytest.MonkeyPatch, content: str) -> dict:
    _repo(r)
    leaf = f"state/subagent-share/{_SESSION}/coordinator-executor.{_AGENT}.md"
    return _payload("Write", r / leaf, r, content=content)


FIXTURES[_WG + "block_hand_authored_sidecar_creation"] = _fixtures(
    deny=[_case(lambda r, mp: _hand_sidecar(r, mp, "no frontmatter\n"), _OV_SIDECAR)],
    allow=[_case(lambda r, mp: _hand_sidecar(r, mp, "---\nagent_type: coordinator:executor\n---\n"), _OV_SIDECAR)],
)

# --- block_home_dir_memo_delivery ---
_OV_HOME_MEMO = "COORDINATOR_OVERRIDE_HOME_MEMO_GUARD"


def _home_memo(r: Path, mp: pytest.MonkeyPatch, rel: str) -> dict:
    home = r / "home"
    home.mkdir(parents=True, exist_ok=True)
    _env(mp, USERPROFILE=str(home), HOME=str(home), CLAUDE_CONFIG_DIR=str(home / ".claude"))
    mp.setattr("pathlib.Path.home", classmethod(lambda cls: home))
    return _payload("Write", home / ".claude" / rel, r, content="x")


FIXTURES[_WG + "block_home_dir_memo_delivery"] = _fixtures(
    deny=[_case(lambda r, mp: _home_memo(r, mp, "cross-repo/m.md"), _OV_HOME_MEMO)],
    allow=[_case(lambda r, mp: _home_memo(r, mp, "notes/m.md"), _OV_HOME_MEMO)],
)

# --- block_oss_mirror_memo_delivery ---
_OV_OSS_MEMO = "COORDINATOR_OVERRIDE_OSS_MIRROR_MEMO_GUARD"


def _oss_memo(r: Path, mp: pytest.MonkeyPatch, rel: str) -> dict:
    from coordinator_core.write_guards import block_oss_mirror_memo_delivery as guard

    mirror = r / "mirror"
    mp.setattr(guard, "read_publish_mirrors", lambda: {"oss": {"path": str(mirror)}})
    return _payload("Write", mirror / rel, r, content="x")


FIXTURES[_WG + "block_oss_mirror_memo_delivery"] = _fixtures(
    deny=[_case(lambda r, mp: _oss_memo(r, mp, "state/cross-repo/m.md"), _OV_OSS_MEMO)],
    allow=[_case(lambda r, mp: _oss_memo(r, mp, "docs/a.md"), _OV_OSS_MEMO)],
)

# --- block_priority_ledger_edit ---
_OV_LEDGER = "COORDINATOR_OVERRIDE_PRIORITY_LEDGER_EDIT"
FIXTURES[_WG + "block_priority_ledger_edit"] = _fixtures(
    deny=[_case(lambda r, mp: _payload("Write", r / "state/priority-ledger/p.yaml", r, content="x"), _OV_LEDGER)],
    allow=[_case(lambda r, mp: _payload("Write", r / "state/other/p.yaml", r, content="x"), _OV_LEDGER)],
)

# --- block_sizing_object_schema_violation ---
_VALID_SIZING = """\
schema: sizing-object
intent: "Do the thing, verbatim."
estimate:
  tshirt: S
  provisional: true
route: dispatch
detents: []
fork: null
xl_exit: null
status: draft
premise:
  provenance: unrecorded
"""
_OV_SIZING = "COORDINATOR_OVERRIDE_SIZING_SCHEMA_BLOCK"


def _sizing(r: Path, content: str) -> dict:
    (r / "state" / "sizings").mkdir(parents=True, exist_ok=True)
    return _payload("Write", r / "state/sizings/2026-09-25-x.yaml", r, content=content)


FIXTURES[_WG + "block_sizing_object_schema_violation"] = _fixtures(
    deny=[_case(lambda r, mp: _sizing(r, _VALID_SIZING + 'pm_resolution: "the PM said so"\n'), _OV_SIZING)],
    allow=[_case(lambda r, mp: _sizing(r, _VALID_SIZING), _OV_SIZING)],
)

# --- grant-record and archive writes by a subagent ---
def _grant_record(r: Path, filename: str, sid: str = "sess-grant01") -> dict:
    _repo(r)
    return _agent(_payload("Write", r / ".git" / "coordinator-sessions" / sid / filename, r, content="{}"))


def _claude_md_grant_name() -> str:
    from coordinator_core.session.claude_md_grant import _GRANT_FILENAME

    return _GRANT_FILENAME


def _em_guard_grant_name() -> str:
    from coordinator_core.session.em_guard_grant import _GRANT_FILENAME

    return _GRANT_FILENAME


FIXTURES[_WG + "block_subagent_grant_record_write"] = _fixtures(
    deny=[lambda r, mp: _grant_record(r, _claude_md_grant_name())],
    allow=[lambda r, mp: _grant_record(r, "other.json")],
)
FIXTURES[_WG + "block_subagent_guard_grant_write"] = _fixtures(
    deny=[lambda r, mp: _grant_record(r, _em_guard_grant_name())],
    allow=[lambda r, mp: _grant_record(r, "other.json")],
)

_OV_ARCHIVE = "COORDINATOR_OVERRIDE_SUBAGENT_ARCHIVE"
FIXTURES[_WG + "block_subagent_archive_write"] = _fixtures(
    deny=[_case(lambda r, mp: _agent(_payload("Write", r / "archive/notes.md", _repo(r), content="x")), _OV_ARCHIVE)],
    allow=[
        _case(
            lambda r, mp: _agent(_payload("Write", r / "archive/daily-summaries/2026-10-01.md", _repo(r), content="x")),
            _OV_ARCHIVE,
        )
    ],
)

# --- guard_settings_json_write ---
def _settings_json(r: Path, mp: pytest.MonkeyPatch, content: str) -> dict:
    config = r / "claude-config"
    _put(config / ".coordinator-hooks-disabled", "")
    _env(mp, CLAUDE_CONFIG_DIR=str(config))
    return _payload("Write", config / "settings.json", r, content=content)


FIXTURES[_WG + "guard_settings_json_write"] = _fixtures(
    deny=[lambda r, mp: _settings_json(r, mp, '{"hooks": {}}')],
    allow=[lambda r, mp: _settings_json(r, mp, '{"model": "x"}')],
)

# --- block_dev_side_mirror_wiki ---
_OV_MIRROR = "COORDINATOR_OVERRIDE_WIKI_MIRROR"


def _mirror_wiki(r: Path, mp: pytest.MonkeyPatch, name: str) -> dict:
    from coordinator_core.write_guards import block_dev_side_mirror_wiki as guard

    home = r / "home"
    (home / ".claude" / "docs" / "wiki").mkdir(parents=True, exist_ok=True)
    plugin = r / "plugin"
    _put(plugin / "docs" / "wiki" / "example.md", "bundled\n")
    _env(mp, HOME=str(home), CLAUDE_PLUGIN_ROOT=str(plugin), COORDINATOR_PLUGIN_ROOT_TRUSTED="1")
    _clear(mp, "CLAUDE_HOME", guard._OVERRIDE_ENV_VAR)
    return _payload("Write", home / ".claude" / "docs" / "wiki" / name, r, content="x")


FIXTURES[_WG + "block_dev_side_mirror_wiki"] = _fixtures(
    deny=[lambda r, mp: _mirror_wiki(r, mp, "example.md")],
    allow=[lambda r, mp: _mirror_wiki(r, mp, "unbundled.md")],
)

# --- block_cutover_phase_hand_edit ---
_OV_CUTOVER = "COORDINATOR_OVERRIDE_CUTOVER_PHASE_HAND_EDIT"
_CUTOVER_RECORD = "---\nsurface: closed_reason\nphase: dual-write\nconfirmed_consumers: []\n---\n\n# cutover\n"


def _cutover(r: Path, old: str, new: str) -> dict:
    _repo(r)
    record = _put(r / "state/roadmap/lifecycle-vocab/cutovers/rec.md", _CUTOVER_RECORD)
    return _payload("Edit", record.relative_to(r).as_posix(), r, old_string=old, new_string=new)


FIXTURES[_WG + "block_cutover_phase_hand_edit"] = _fixtures(
    deny=[_case(lambda r, mp: _cutover(r, "phase: dual-write", "phase: retiring"), _OV_CUTOVER)],
    allow=[_case(lambda r, mp: _cutover(r, "confirmed_consumers: []", "confirmed_consumers: [a]"), _OV_CUTOVER)],
)

# --- block_consumed_handoff_edit ---
_OV_CONSUMED = "COORDINATOR_OVERRIDE_CONSUMED_HANDOFF_EDIT"
_SESSION_VARS = ("COORDINATOR_SESSION_ID", "CLAUDE_CODE_SESSION_ID", "CLAUDE_SESSION_ID")


def _consumed_handoff(r: Path, status_block: str) -> dict:
    _repo(r)
    _put(r / "state/handoffs/2026-07-20_120000_abc.md", f"---\n{status_block}title: t\n---\n\n# t\n")
    return _payload(
        "Edit", "state/handoffs/2026-07-20_120000_abc.md", r, old_string="# t", new_string="# t\n\nmore"
    )


FIXTURES[_WG + "block_consumed_handoff_edit"] = _fixtures(
    deny=[
        _case(
            lambda r, mp: _consumed_handoff(r, "status: claimed\nclaimed_by: some-prior-session\n"),
            _OV_CONSUMED,
            *_SESSION_VARS,
        )
    ],
    allow=[_case(lambda r, mp: _consumed_handoff(r, "status: open\n"), _OV_CONSUMED, *_SESSION_VARS)],
)

# --- block_memo_status_hand_edit ---
# Deny rides the indeterminate-liveness path: no resolvable git dir fails toward deny.
_OV_MEMO_STATUS = "COORDINATOR_OVERRIDE_MEMO_STATUS_HAND_EDIT"


def _memo_status(r: Path, resolvable: bool, mp: pytest.MonkeyPatch) -> dict:
    if resolvable:
        _repo(r)
    else:
        from coordinator_core.write_guards import block_memo_status_hand_edit as guard

        mp.setattr(guard, "_resolve_git_common_dir", lambda cwd: None)
    memo = _put(r / "cross-repo/inbox/m.md", "---\nstatus: open\n---\n\nbody\n")
    return _payload(
        "Edit", memo.relative_to(r).as_posix(), r, old_string="status: open", new_string="status: actioned"
    )


FIXTURES[_WG + "block_memo_status_hand_edit"] = _fixtures(
    deny=[_case(lambda r, mp: _memo_status(r, False, mp), _OV_MEMO_STATUS)],
    allow=[_case(lambda r, mp: _memo_status(r, True, mp), _OV_MEMO_STATUS)],
)

# --- block_unauthorized_claude_md_write ---
_OV_CLAUDE_MD = "COORDINATOR_OVERRIDE_CLAUDE_MD_WRITE"


def _claude_md(r: Path, agent: bool) -> dict:
    _repo(r)
    payload = _payload("Write", r / "CLAUDE.md", r, content="# grown\n" * 50)
    return _agent(payload) if agent else payload


FIXTURES[_WG + "block_unauthorized_claude_md_write"] = _fixtures(
    deny=[_case(lambda r, mp: _claude_md(r, True), _OV_CLAUDE_MD)],
    allow=[_case(lambda r, mp: _claude_md(r, False), _OV_CLAUDE_MD)],
)

# --- p4_checkout_before_edit ---
def _p4(r: Path, mp: pytest.MonkeyPatch, p4_repo: bool) -> dict:
    import os
    import stat

    from coordinator_core.p4 import workspace
    from coordinator_core.write_guards import p4_checkout_before_edit as guard

    _repo(r)
    target = _put(r / "file.txt", "hi")
    os.chmod(target, stat.S_IREAD)
    mp.setattr(workspace, "is_p4_repo", lambda root: p4_repo)
    mp.setattr(guard, "_resolve_repo_key", lambda root: "fixture")

    def unregistered(repo_key: str) -> Any:
        raise workspace.P4WorkspaceUnregistered(repo_key)

    mp.setattr(workspace, "identity", unregistered)
    return _payload("Edit", target, r, old_string="hi", new_string="ho")


FIXTURES[_WG + "p4_checkout_before_edit"] = _fixtures(
    deny=[lambda r, mp: _p4(r, mp, True)],
    allow=[lambda r, mp: _p4(r, mp, False)],
)

# --- block_em_strict_dispatch_code_write ---
def _em_strict(r: Path, mp: pytest.MonkeyPatch, rel: str) -> dict:
    from coordinator_core.write_guards import block_em_strict_dispatch_code_write as guard

    _repo(r)
    _clear(mp, "COORDINATOR_STRICT_DISPATCH")
    mp.setattr(guard, "_is_subagent_session", lambda sid, root: False)
    mp.setattr(guard, "_session_touched_sizing_files", lambda sid, root: [])
    payload = _payload("Write", r / rel, r, content="x = 1")
    payload["session_id"] = "sess-12345678"
    return payload


FIXTURES[_WG + "block_em_strict_dispatch_code_write"] = _fixtures(
    deny=[lambda r, mp: _em_strict(r, mp, "src/thing.py")],
    allow=[lambda r, mp: _em_strict(r, mp, "docs/notes.md")],
)

# --- block_subagent_plan_body_write ---
_OV_PLAN_BODY = "COORDINATOR_OVERRIDE_SUBAGENT_PLAN_BODY"


def _plan_body(r: Path, subagent_type: str) -> dict:
    _repo(r)
    _dispatch_row(r, subagent_type)
    return _agent(_payload("Edit", r / "docs/plans/p.md", r, old_string="x", new_string="y"))


FIXTURES[_WG + "block_subagent_plan_body_write"] = _fixtures(
    deny=[_case(lambda r, mp: _plan_body(r, "AMBIGUOUS"), _OV_PLAN_BODY)],
    allow=[_case(lambda r, mp: _plan_body(r, "coordinator:executor"), _OV_PLAN_BODY)],
)

# --- validate_frontmatter_schema_deny ---
_OV_OWN_INBOX = "COORDINATOR_OVERRIDE_OWN_INBOX"
_MEMO = (
    "---\nfrom: {frm}\nto: {to}\ntopic: test\ntitle: t\ncreated: 2026-07-29\n"
    "status: open\ndelivery_mode: receiver-repo\n---\nbody"
)


def _schema_memo(r: Path, mp: pytest.MonkeyPatch, frm: str, to: str) -> dict:
    from coordinator_core.write_guards import validate_frontmatter_schema_deny as guard

    repo = _repo(r / "fixture-repo")
    mp.setattr(guard, "read_content_root", lambda: str(r / "content-root"))
    _clear(mp, "COORDINATOR_SCHEMA_STRICT")
    path = repo / "cross-repo" / "inbox" / "2099-01-01-fixture-memo.md"
    return _payload("Write", path, repo, content=_MEMO.format(frm=frm, to=to))


FIXTURES[_WG + "validate_frontmatter_schema_deny"] = _fixtures(
    deny=[_case(lambda r, mp: _schema_memo(r, mp, "fixture-repo-em", "other-em"), _OV_OWN_INBOX)],
    allow=[_case(lambda r, mp: _schema_memo(r, mp, "other-em", "fixture-repo-em"), _OV_OWN_INBOX)],
)

# --- guard_memory_store_cap ---
def _memory_index(r: Path, mp: pytest.MonkeyPatch, rows: int) -> dict:
    home = r / "home"
    memory = home / ".claude" / "projects" / "-Some-project" / "memory"
    memory.mkdir(parents=True, exist_ok=True)
    (r / "lessons-root").mkdir(exist_ok=True)
    _env(mp, HOME=str(home), USERPROFILE=str(home), QUEUE_APPEND_OUTPUT_ROOT=str(r / "lessons-root"))
    _clear(mp, "CLAUDE_HOME", "CLAUDE_CONFIG_DIR")
    body = "# Memory Index\n\n" + "\n".join(f"- [x{i}](slug{i}.md) short" for i in range(rows))
    return _payload("Write", memory / "MEMORY.md", r, content=body)


FIXTURES[_WG + "guard_memory_store_cap"] = _fixtures(
    deny=[lambda r, mp: _memory_index(r, mp, 6)],
    allow=[lambda r, mp: _memory_index(r, mp, 5)],
)

# --- nudge_prose_queue_creation ---
_OV_PROSE_QUEUE = "COORDINATOR_OVERRIDE_PROSE_QUEUE_CREATION"


def _prose_queue(r: Path, content: str) -> dict:
    return _payload("Write", r / "state" / "improvement-queue.md", r, content=content)


FIXTURES[_WG + "nudge_prose_queue_creation"] = _fixtures(
    deny=[_case(lambda r, mp: _prose_queue(r, "- 2026-10-01 | an entry\n"), _OV_PROSE_QUEUE)],
    allow=[_case(lambda r, mp: _prose_queue(r, "# Queue\n\nno entries yet\n"), _OV_PROSE_QUEUE)],
)

# --- nudge_improvement_queue_write ---
_OV_QUEUE_PUNT = "COORDINATOR_QUEUE_PUNT"


def _improvement_queue(r: Path, content: str) -> dict:
    return _payload("Write", r / "state" / "improvement-queue" / "q.yaml", r, content=content)


FIXTURES[_WG + "nudge_improvement_queue_write"] = _fixtures(
    deny=[_case(lambda r, mp: _improvement_queue(r, "title: t\n"), _OV_QUEUE_PUNT)],
    allow=[
        _case(
            lambda r, mp: _improvement_queue(r, "title: t\njustification: genuinely cross-cutting, needs a separate plan\n"),
            _OV_QUEUE_PUNT,
        )
    ],
)

# --- check_claude_md_size ---
def _claude_md_size(r: Path, oversize: bool) -> dict:
    from coordinator_core.write_guards.check_claude_md_size import HARD_LIMIT_BYTES

    _repo(r)
    _put(r / ".coordinator-dev-repo", "")
    content = "x" * (HARD_LIMIT_BYTES + 1) if oversize else "# small\n"
    return _payload("Write", r / "coordinator" / "CLAUDE.md", r, content=content)


FIXTURES[_WG + "check_claude_md_size"] = _fixtures(
    deny=[lambda r, mp: _claude_md_size(r, True)],
    allow=[lambda r, mp: _claude_md_size(r, False)],
)

# --- block_illegal_filename ---
_OV_FILENAME = "COORDINATOR_OVERRIDE_ILLEGAL_FILENAME"
FIXTURES[_WG + "block_illegal_filename"] = _fixtures(
    deny=[_case(lambda r, mp: _payload("Write", r / "bad|name.md", r, content="x"), _OV_FILENAME)],
    allow=[_case(lambda r, mp: _payload("Write", r / "good-name.md", r, content="x"), _OV_FILENAME)],
)


# --- anchor-bound hard-deny guards: block_home_root_write, block_drive_root_new_dir_write,
# block_derived_global_doctrine_write, bump_out_of_repo_tool_write ---
# Each denies only against the real home, a drive root, ~/.claude or a launch anchor. A case re-points
# that anchor at `root` and lifts the guard into the deny floor (they ship in `deny_registry.ADVISORY_BY_RULING`,
# so `apply_guard_level` would otherwise return an advisory). Nothing real is touched.
def _floor(mp: pytest.MonkeyPatch, kebab_name: str) -> None:
    from coordinator_core import machine_profile

    mp.setattr(machine_profile, "FLOOR_GUARDS", machine_profile.FLOOR_GUARDS | {kebab_name})


def _fake_home(r: Path, mp: pytest.MonkeyPatch) -> Path:
    from coordinator_core.testing.home_sandbox import sandbox_home

    return sandbox_home(mp, r / "home")


def _home_root(r: Path, mp: pytest.MonkeyPatch, name: str) -> dict:
    _floor(mp, "block-home-root-write")
    home = _fake_home(r, mp)
    return _payload("Write", home / name, r, content="x")


FIXTURES[_WG + "block_home_root_write"] = _fixtures(
    deny=[lambda r, mp: _home_root(r, mp, "stray.txt")],
    allow=[lambda r, mp: _home_root(r, mp, ".gitconfig")],
)


def _drive_root(r: Path, mp: pytest.MonkeyPatch, entry: str) -> dict:
    _floor(mp, "block-drive-root-new-dir-write")
    _fake_home(r, mp)
    drive = r / "drive"
    (drive / "existing").mkdir(parents=True, exist_ok=True)
    _env(mp, COORDINATOR_DRIVE_ROOTS=str(drive), COORDINATOR_DRIVE_ROOT_BASELINE=str(r / "base.json"))
    _clear(mp, "COORDINATOR_ALLOW_DRIVE_ROOT_NEW_DIR")
    return _payload("Write", drive / entry / "x.txt", r, content="x")


FIXTURES[_WG + "block_drive_root_new_dir_write"] = _fixtures(
    deny=[lambda r, mp: _drive_root(r, mp, "brand-new")],
    allow=[lambda r, mp: _drive_root(r, mp, "existing")],
)


def _derived_doctrine(r: Path, mp: pytest.MonkeyPatch, derived: bool) -> dict:
    from coordinator_core.write_guards import block_derived_global_doctrine_write as guard

    _floor(mp, "block-derived-global-doctrine-write")
    _clear(mp, "COORDINATOR_OVERRIDE_DERIVED_GLOBAL_DOCTRINE_WRITE")
    home = _fake_home(r, mp)
    mp.setattr(guard, "_home_candidates", lambda: [str(home)])
    mp.setattr(guard, "registry_get", lambda key: str(r / "content-root"))
    target = home / ".claude" / "CLAUDE.md" if derived else r / "content-root" / "global-doctrine" / "CLAUDE.md"
    return _payload("Write", target, r, content="x")


FIXTURES[_WG + "block_derived_global_doctrine_write"] = _fixtures(
    deny=[lambda r, mp: _derived_doctrine(r, mp, True)],
    allow=[lambda r, mp: _derived_doctrine(r, mp, False)],
)


def _bump_out_of_repo(r: Path, mp: pytest.MonkeyPatch, outside: bool) -> dict:
    from coordinator_core import machine_profile
    from coordinator_core.bash_guards import _write_bump_applicability as applicability
    from coordinator_core.bash_guards import _write_bump_session_start as session_start
    from coordinator_core.tests.git_seed import seeded_repo

    _floor(mp, "bump-out-of-repo-tool-write")
    home = _fake_home(r, mp)
    _put(home / ".coordinator-claude-settings" / "settings.json", "{}\n")
    sid = f"sess-{abs(hash(str(r))):x}"
    mp.setenv("MACHINE_LOCAL_COORDINATOR_FEATURE_OUT_OF_REPO_WRITE_BLOCK", "on")
    machine_profile.reset_cache()
    elsewhere = r / "unrelated-temp-root"
    elsewhere.mkdir(parents=True, exist_ok=True)
    mp.setattr(applicability.tempfile, "gettempdir", lambda: str(elsewhere))
    mp.setattr(applicability, "_posix_tmp_literal", lambda: str(elsewhere))
    _clear(mp, "TMPDIR", "TEMP", "TMP")
    own = seeded_repo(r / "own-repo", name="Test")
    session_start.write_session_start_record(sid, launch_cwd=str(own))
    target = r / "stray-dir" / "x.txt" if outside else own / "x.txt"
    payload = _payload("Write", target, own)
    payload["session_id"] = sid
    return payload


FIXTURES[_WG + "bump_out_of_repo_tool_write"] = _fixtures(
    deny=[lambda r, mp: _bump_out_of_repo(r, mp, True)],
    allow=[lambda r, mp: _bump_out_of_repo(r, mp, False)],
)

