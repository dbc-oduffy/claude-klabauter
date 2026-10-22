"""The reply a plan owes the inbox memo its own spine row closes, sent before the memo resolves.

``send_close_reply`` reads one plan and one inbox memo and sends one ``in_reply_to`` memo through
the ``memo.draft`` / ``memo.compose`` / ``memo.send`` ops. It spawns no process and globs no corpus.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Optional

from coordinator_core.ops.dispatch_emit import run_close
from coordinator_core.ops.dispatch_emit.run_close import LegResult

REPLY_KIND = "fyi"
_SHA = re.compile(r"(?<![0-9A-Za-z_])[0-9a-f]{7,40}(?![0-9A-Za-z_])")
_NON_SLUG = re.compile(r"[^a-z0-9]+")


def cited_shas(text: str) -> tuple[str, ...]:
    """Ordered unique hex tokens of 7-40 chars bounded by non-word characters, each with a digit."""
    seen: dict[str, None] = {}
    for match in _SHA.finditer(text or ""):
        token = match.group(0)
        if any(c.isdigit() for c in token):
            seen.setdefault(token)
    return tuple(seen)


def _plan_rows(root: Path, plan_rel: str) -> list:
    from coordinator_core.frontmatter.body_blocks import LocateStatus, locate_fenced_block
    from coordinator_core.ops.dispatch_emit.plan_row_coding import yaml_load
    from coordinator_core.ops.dispatch_emit.worktree_snapshot import read_rel

    text = read_rel(root, plan_rel)
    if text is None:
        return []
    located = locate_fenced_block(text.replace("\r\n", "\n"))
    if located.status != LocateStatus.LOCATED:
        return []
    try:
        rows = yaml_load(located.body) or []
    except Exception:  # noqa: BLE001 -- an unparseable spine owes no reply
        return []
    return [r for r in rows if isinstance(r, dict)] if isinstance(rows, list) else []


def _memo_frontmatter(path: Path) -> dict:
    from coordinator_core.frontmatter.primitives import split_frontmatter
    from coordinator_core.ops.dispatch_emit.plan_row_coding import yaml_load

    try:
        split = split_frontmatter(path.read_text(encoding="utf-8").replace("\r\n", "\n"))
        fm = (yaml_load(split.fm_text) or {}) if split is not None else {}
    except Exception:  # noqa: BLE001 -- an unreadable memo reads as no frontmatter
        return {}
    return fm if isinstance(fm, dict) else {}


def _refusal(reply: Any) -> Optional[str]:
    """None when the op reply is a clean ``exit_code`` 0; else its first failure reason."""
    if isinstance(reply, dict) and reply.get("exit_code") == 0:
        return None
    if isinstance(reply, dict):
        failed = reply.get("failed") or []
        if failed and isinstance(failed[0], dict) and failed[0].get("reason"):
            return str(failed[0]["reason"])
        return f"exit_code {reply.get('exit_code')}"
    return "no reply"


def send_close_reply(
    root: Path,
    plan_rel: Optional[str],
    source_memo: str,
    run_id: str,
    terminal_sha: Optional[str],
) -> Optional[LegResult]:
    """Reply to ``source_memo`` naming the shas its closing row cites; ``None`` when none is owed.

    Owed only when the plan carries a row whose ``inbound_close_path`` is ``source_memo``. The
    sender receipt for the reply topic existing means it was already sent: no dispatch.
    """
    from coordinator_core.memo_corpus import memo_corpus_root
    from coordinator_core.ops.dispatch_emit.memo_rows import inbound_close_path, memo_receipt_exists

    root = Path(root)
    if not plan_rel or not source_memo:
        return None
    target = source_memo.replace("\\", "/")
    rows = [r for r in _plan_rows(root, plan_rel) if inbound_close_path(r) == target]
    if not rows:
        return None

    def done(status: str, text: str) -> LegResult:
        return LegResult(status, run_close.scrub_detail(text, root))

    shas: list = []
    for row in rows:
        shas.extend(s for s in cited_shas(f"{row.get('title') or ''}\n{row.get('body') or ''}") if s not in shas)
    if not shas and terminal_sha:
        shas = [terminal_sha[:10]]
    if not shas:
        return done("failed", "reply owed but the closing row cites no sha and the run has no terminal commit")

    name = Path(target).name
    memo = Path(memo_corpus_root(str(root))) / "inbox" / name
    fm = _memo_frontmatter(memo)
    receiver = fm.get("from")
    if not isinstance(receiver, str) or not receiver.strip():
        return done("failed", f"reply owed but memo {name} carries no from: receiver")

    topic = _NON_SLUG.sub("-", f"re-{Path(name).stem}".lower()).strip("-")
    if memo_receipt_exists(root, topic):
        return done("ran", f"reply {topic} already sent")

    from coordinator_core.warm.entry_seam import reentrant_dispatch

    title = fm.get("title") if isinstance(fm.get("title"), str) and fm.get("title") else Path(name).stem
    body = f"Run {run_id} closed this memo. Landed at: {', '.join(shas)}."
    common_dir = root / ".git"
    steps = (
        ("memo.draft", {"dry_run": False, "topic": topic, "to": receiver.strip(), "kind": REPLY_KIND,
                        "title": f"Re: {title}", "in_reply_to": name}),
        ("memo.compose", {"dry_run": False, "topic": topic, "body": body}),
        ("memo.send", {"dry_run": False, "topic": topic}),
    )
    for op, params in steps:
        try:
            reason = _refusal(reentrant_dispatch(op, params, repo_root=common_dir))
        except Exception as exc:  # noqa: BLE001 -- recorded as the leg's failure
            return done("failed", f"{op} raised {type(exc).__name__}")
        # An earlier refire may have staged the draft before it failed; compose rewrites its body.
        if reason is not None and not (op == "memo.draft" and reason.startswith("collision")):
            return done("failed", f"{op} refused: {reason}")
    return done("ran", f"reply {topic} sent naming {', '.join(shas)}")
