"""
coordinator_core.subagent_sandbox.harvest_exit_interviews -- grep-and-
concatenate harvest for the ``## Exit interview`` sidecar section that
provision_report.py (DR-047's spawn-time seam) seeds verbatim into every
eligible agent's run-report sidecar, across BOTH of that module's homes:
the session-keyed ``state/subagent-share/<session_id>/`` tree, and the
plan-derivable ``state/plan-sidecars/`` tree the four G2 emitters use
(canonical spec § 2.7, `state/subagent-share/conductor/seam-adjudication.md`,
Example-doctrine-repo). Both trees carry the identical ``## Exit interview`` shape --
this module walks them the same way and folds the results into one report.

This is deliberately the smallest useful tool: walk the sidecar tree(s),
pull out each file's Exit interview section body, skip the ones nobody
answered, and print the rest to stdout with a short provenance header.
No database, no index, no query language, no claude-klabauter op registration --
a baton holder pipes this to a file or a pager before writing the next
dispatch brief.

``state/plan-sidecars/`` is flat (one file per plan+lens, no per-session
subdirectory -- the path is plan-derivable, not session-derivable) and is
therefore harvested in full regardless of ``--session``: a plan-sidecar
file has no session to filter by. Per the Z4 one-sidecar resolution, each
of the four emitters' provisioned doc IS the lens output -- there is no
separate vestigial subagent-share file for a plan-side dispatch to also
harvest.

Spec backlink: docs/plans/2026-07-13-subagent-run-report-autoprovision.md
(the sidecar this module reads); this module itself is the harvest leg
of coordinator baton G0 (agent citizenship), leg (c).
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from typing import List, Optional, Sequence, Tuple

#: Matches the Exit interview heading through the next top-level heading
#: (or end of file) -- DOTALL so '.' spans newlines, non-greedy so we stop
#: at the *next* '## ' rather than swallowing the rest of the document.
_EXIT_INTERVIEW_RE = re.compile(
    r"^##\s+Exit interview\s*\n(?P<body>.*?)(?=^## |\Z)",
    re.MULTILINE | re.DOTALL,
)

#: Matches a single ``key: value`` frontmatter line -- just enough to pull
#: agent_type out of the ``---`` YAML-ish block provision_report.py writes;
#: not a general YAML parser.
_FRONTMATTER_LINE_RE = re.compile(r"^agent_type:\s*(?P<value>.+?)\s*$", re.MULTILINE)

#: The four verbatim discovery-question prompts provision_report.py seeds.
#: A section is "empty/unanswered" if, once these prompts (and blank
#: lines/bullet markers) are stripped out, nothing is left.
_QUESTION_PROMPTS = (
    "What did you have to work out that the brief could have told you?",
    "What did you grep, read, or probe that turned out to be a dead end — "
    "and what were you actually looking for?",
    "Where did your tool access, permissions, or output contract fight you? "
    "What would you have reached for if it existed?",
    "Anything you wanted to say and had nowhere to put?",
)


def _extract_frontmatter_agent_type(text: str) -> Optional[str]:
    """Pull the ``agent_type:`` frontmatter value out of a sidecar doc, or
    ``None`` if the doc has no frontmatter block or no such key."""
    if not text.startswith("---"):
        return None
    end = text.find("\n---", 3)
    frontmatter = text[:end] if end != -1 else text
    match = _FRONTMATTER_LINE_RE.search(frontmatter)
    if match is None:
        return None
    return match.group("value").strip()


def _extract_exit_interview_body(text: str) -> Optional[str]:
    """Return the raw body of the ``## Exit interview`` section, or ``None``
    if the doc has no such section."""
    match = _EXIT_INTERVIEW_RE.search(text)
    if match is None:
        return None
    return match.group("body").strip("\n")


def _is_answered(body: str) -> bool:
    """True if ``body`` contains any content beyond the four verbatim
    question prompts, blank lines, and leading bullet markers."""
    remainder = body
    for prompt in _QUESTION_PROMPTS:
        remainder = remainder.replace(prompt, "")
    for line in remainder.splitlines():
        stripped = line.strip().lstrip("-").strip()
        if stripped:
            return True
    return False


def _iter_sidecar_files(share_root: Path, session: Optional[str]) -> List[Path]:
    """Enumerate session-keyed sidecar markdown files under ``share_root``,
    optionally restricted to a single ``<session_id>`` subdirectory."""
    if not share_root.is_dir():
        return []
    if session is not None:
        session_dir = share_root / session
        if not session_dir.is_dir():
            return []
        return sorted(session_dir.glob("*.md"))
    return sorted(share_root.glob("*/*.md"))


def _iter_plan_sidecar_files(plan_sidecars_root: Path) -> List[Path]:
    """Enumerate plan-derivable sidecar markdown files under
    ``plan_sidecars_root`` (``state/plan-sidecars/``, flat -- no
    per-session subdirectory to filter by, so always harvested in full)."""
    if not plan_sidecars_root.is_dir():
        return []
    return sorted(plan_sidecars_root.glob("*.md"))


def harvest(repo_root: Path, session: Optional[str]) -> Tuple[str, int, int]:
    """Walk both sidecar trees -- ``repo_root/state/subagent-share``
    (session-keyed, optionally filtered by ``session``) and
    ``repo_root/state/plan-sidecars`` (plan-derivable, always harvested in
    full) -- and build the concatenated exit-interview report.

    Returns (report_text, included_count, skipped_empty_count).
    """
    share_root = repo_root / "state" / "subagent-share"
    plan_sidecars_root = repo_root / "state" / "plan-sidecars"
    files = _iter_sidecar_files(share_root, session) + _iter_plan_sidecar_files(plan_sidecars_root)

    sections: List[str] = []
    skipped_empty = 0

    for path in files:
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue

        body = _extract_exit_interview_body(text)
        if body is None:
            continue

        if not _is_answered(body):
            skipped_empty += 1
            continue

        agent_type = _extract_frontmatter_agent_type(text)
        try:
            rel_path = path.relative_to(repo_root)
        except ValueError:
            rel_path = path

        header_bits = [rel_path.as_posix()]
        if agent_type is not None:
            header_bits.append(f"agent_type={agent_type}")
        header = " -- ".join(header_bits)

        sections.append(f"### {header}\n\n{body}\n")

    report_text = "\n".join(sections)
    return report_text, len(sections), skipped_empty


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="coordinator_core.subagent_sandbox.harvest_exit_interviews",
        description="Concatenate answered '## Exit interview' sidecar sections "
        "for a workstream into one readable report on stdout.",
    )
    parser.add_argument(
        "--cwd",
        dest="cwd",
        default=".",
        help="Repo root under which state/subagent-share/ and state/plan-sidecars/ live (default: '.').",
    )
    parser.add_argument(
        "--session",
        dest="session",
        default=None,
        help="Restrict the harvest to a single session_id subdirectory "
        "(default: harvest across all sessions).",
    )
    args = parser.parse_args(argv)

    repo_root = Path(args.cwd)
    report_text, included, skipped_empty = harvest(repo_root, args.session)

    if report_text:
        print(report_text)
    print(
        f"-- harvested {included} answered exit interview(s), "
        f"skipped {skipped_empty} empty/unanswered",
        file=sys.stderr,
    )

    return 0


if __name__ == "__main__":
    sys.exit(main())
