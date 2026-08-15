# Unix shebang — was generator-owned by gen-launcher-shim.py --ensure-unix; that mode was retired 2026-07-28 (POSIX-EXEC-ASSUMPTION-GUARD, PM ruling) and no longer regenerates this line.
"""
coordinator_core.subagent_sandbox.detect_unfilled_sidecar -- the DETECTION
leg of the run-report sidecar contract, closing the second of two sub-
defects observed live 2026-08-15 (state/subagent-share/
4b4274fc-4bfe-4084-a986-c78b5bae8fad/coordinatorexecutor-0da3fc1f.md): a
dispatched agent went idle with its provision_report.py-scaffolded sidecar
untouched -- ``status: open``, every body section still the pristine
template -- and nothing surfaced that to the dispatching EM. The EM only
caught it by manually reconstructing ground truth from ``git diff`` plus a
4-minute test run.

WHAT THIS IS NOT: a write-time enforcer. Nothing forces an agent to fill
its sidecar (that is sub-defect 1, out of scope here -- see this module's
call site's dispatch report for the named residual). This module is a
cheap, EM-run READ that makes the unfilled state visible on demand,
instead of silent until an EM happens to diff the tree by hand.

Unfilled-body detection is CONTENT-based, not type-based: it strips the
frontmatter block, then every HTML comment, blank line, ``## heading``
line, and the run-report scaffold's own literal unchecked checklist line
(derived off ``provision_report._build_run_report_doc_text`` -- see
``_derive_scaffold_checkbox_lines``) -- and calls the sidecar unfilled if
nothing survives that strip. This works uniformly across every
TEMPLATE_TYPES shape (run-report/review-findings/assessment/
staff-eng-review) without hand-duplicating each template's literal prose,
because the scaffold's only universal properties are "heading, comment, or
blank" -- any agent-authored sentence, even one word, survives the strip
and flips the verdict to filled. The checklist match is anchored to the
literal scaffold line, not the bare ``- [ ]`` marker, so an agent's own
genuine unchecked TODO is real content and is never stripped.

A sidecar is FLAGGED (the miss this module exists to catch) when BOTH:
  1. frontmatter ``status:`` is still ``open`` (the scaffold default --
     see provision_report._frontmatter), AND
  2. the body is unfilled per the strip above.

Negative-spec: this module never reads or calls
``coordinator_core.session.liveness`` -- unlike the reaper
(reap-stale-subagent-sidecars.py), which gates deletion on the OWNING
session being dead, this is a live-session diagnostic the EM runs against
its OWN just-returned dispatch, where the session is typically still the
current one. Gating on liveness here would suppress the exact case this
tool exists to catch.

Spec backlink: the 2026-08-15 incident report is this module's own design
brief (see dispatching EM's chunk description); no prior plan names this
module, so there is no separate spec doc to backlink beyond the incident.
"""

from __future__ import annotations

import argparse
import glob
import os
import re
import sys
from dataclasses import dataclass
from typing import List, Optional, Sequence

from coordinator_core.subagent_sandbox.provision_report import (
    _build_run_report_doc_text,
    _exit_interview_section,
)

#: Matches an HTML comment, DOTALL so it can span the (rare) multi-line case.
_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)


def _derive_scaffold_checkbox_lines() -> tuple:
    """Derive the literal unchecked-checklist scaffold line(s) straight off
    ``provision_report._build_run_report_doc_text()`` -- the only
    ``TEMPLATE_TYPES`` shape that emits an unchecked checkbox -- rather than
    hand-duplicating the text here (same anti-drift rationale as
    ``_derive_exit_interview_prompts``). Anchoring to the literal scaffold
    line, not the bare ``- [ ]`` marker, matters: an agent's own genuine
    unchecked TODO (``- [ ] follow-up: verify X``) must read as real content,
    not get silently stripped and misreported as an unfilled sidecar."""
    doc = _build_run_report_doc_text("placeholder-agent", "1970-01-01T00:00:00+00:00")
    lines = []
    for line in doc.splitlines():
        stripped = line.strip()
        if stripped.startswith("- [ ]"):
            lines.append(stripped)
    return tuple(lines)


_SCAFFOLD_CHECKBOX_LINES = _derive_scaffold_checkbox_lines()


def _derive_exit_interview_prompts() -> tuple:
    """Derive the four verbatim exit-interview bullet prompts straight off
    ``provision_report._exit_interview_section()`` -- the single literal
    source of truth every template inherits -- rather than hand-duplicating
    them here (same anti-drift rationale as
    harvest_exit_interviews._derive_question_prompts, which this mirrors).
    An unfilled scaffold's exit-interview section is ONLY these four
    bullets, so without stripping them ``is_unfilled_body`` would treat
    every untouched sidecar as filled -- the opposite of this module's
    purpose."""
    prompts = []
    for line in _exit_interview_section().splitlines():
        stripped = line.strip()
        if stripped.startswith("- "):
            prompts.append(stripped)
    return tuple(prompts)


_EXIT_INTERVIEW_PROMPTS = _derive_exit_interview_prompts()


def split_frontmatter(text: str) -> str:
    """Return everything after the frontmatter's closing ``---`` fence, or
    the whole text unchanged if it has no opening fence."""
    if not text.startswith("---"):
        return text
    end = text.find("\n---", 3)
    if end == -1:
        return ""
    # Skip past the "\n---" fence line itself.
    rest = text[end + 4:]
    return rest.split("\n", 1)[1] if "\n" in rest else ""


def is_unfilled_body(text: str) -> bool:
    """True if ``text`` (a sidecar's full contents) has no body content
    beyond frontmatter, headings, HTML comments, blank lines, and unchecked
    checklist scaffolding -- i.e. an agent never wrote into it."""
    body = split_frontmatter(text)
    body = _COMMENT_RE.sub("", body)
    for line in body.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("##"):
            continue
        if stripped in _SCAFFOLD_CHECKBOX_LINES:
            continue
        if stripped in _EXIT_INTERVIEW_PROMPTS:
            continue
        return False
    return True


def _fm_status(text: str) -> str:
    for line in text.split("\n", 40)[:40]:
        if line.strip() == "---":
            continue
        if line.startswith("status:"):
            return line[len("status:"):].strip().strip("'\"")
    return ""


@dataclass(frozen=True)
class SidecarVerdict:
    path: str
    status: str
    unfilled: bool

    @property
    def flagged(self) -> bool:
        return self.status == "open" and self.unfilled


def scan_paths(paths: Sequence[str]) -> List[SidecarVerdict]:
    """Read each path in ``paths`` and return its verdict. A path that
    cannot be read is skipped (never raises) -- this is a best-effort
    diagnostic, not a gate."""
    verdicts: List[SidecarVerdict] = []
    for path in paths:
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                text = fh.read()
        except OSError:
            continue
        verdicts.append(
            SidecarVerdict(path=path, status=_fm_status(text), unfilled=is_unfilled_body(text))
        )
    return verdicts


def scan_session_dir(repo_root: str, session_id: str) -> List[SidecarVerdict]:
    """Scan every sidecar under ``state/subagent-share/<session_id>/``.

    ``session_id`` is validated to resolve inside ``state/subagent-share/``
    before globbing -- read-only usage caps the impact at local information
    disclosure, but a ``--session ../../..``-shaped value should not be able
    to walk the scan outside the intended tree. A value that resolves
    outside is treated as "no sidecars found" rather than raising, matching
    this module's best-effort-diagnostic contract."""
    share_root = os.path.abspath(os.path.join(repo_root, "state", "subagent-share"))
    session_dir = os.path.abspath(os.path.join(share_root, session_id))
    if os.path.commonpath([session_dir, share_root]) != share_root:
        return []
    paths = sorted(glob.glob(os.path.join(session_dir, "*.md")))
    return scan_paths(paths)


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="coordinator_core.subagent_sandbox.detect_unfilled_sidecar",
        description=(
            "Flag run-report sidecars still at status: open with no agent-authored "
            "body content -- the dispatched-agent-went-silent case."
        ),
    )
    parser.add_argument(
        "--cwd", dest="cwd", default=".",
        help="Repo root state/subagent-share/ lives under (default: '.').",
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument(
        "--session", dest="session", default=None,
        help="Scan every sidecar under state/subagent-share/<session>/.",
    )
    group.add_argument(
        "--path", dest="paths", action="append", default=None,
        help="Scan one specific sidecar path (repeatable).",
    )
    args = parser.parse_args(argv)

    if args.session is not None:
        verdicts = scan_session_dir(args.cwd, args.session)
    else:
        verdicts = scan_paths(args.paths)

    flagged = [v for v in verdicts if v.flagged]

    for v in verdicts:
        marker = "FLAGGED (open, unfilled)" if v.flagged else f"ok (status={v.status or 'unknown'})"
        print(f"{v.path}: {marker}")

    if not verdicts:
        print("-- no sidecars found", file=sys.stderr)
    elif flagged:
        print(
            f"-- {len(flagged)} of {len(verdicts)} sidecar(s) flagged: status still open, "
            "no agent-authored content",
            file=sys.stderr,
        )
    else:
        print(f"-- {len(verdicts)} sidecar(s) checked, none flagged", file=sys.stderr)

    return 1 if flagged else 0


if __name__ == "__main__":
    sys.exit(main())
