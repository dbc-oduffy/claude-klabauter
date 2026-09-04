"""
coordinator_core.subagent_sandbox.harvest_exit_interviews -- grep-and-
concatenate harvest for the ``## Exit interview`` sidecar section that
provision_report.py (DR-047's spawn-time seam) seeds verbatim into every
eligible agent's run-report sidecar, across BOTH of that module's homes:
the session-keyed ``state/subagent-share/<session_id>/`` tree, and the
plan-derivable ``state/plan-sidecars/`` tree the four G2 emitters use
(canonical spec § 2.7, `state/subagent-share/conductor/seam-adjudication.md`,
DoE-claude). Both trees carry the identical ``## Exit interview`` shape --
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

Spec backlink: pln-claude-klabauter-subagent-run-report-aut-f51428
(the sidecar this module reads); this module itself is the harvest leg
of coordinator baton G0 (agent citizenship), leg (c).
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from typing import List, Optional, Sequence, Tuple

from coordinator_core.session.machinery_paths import machinery_root as _machinery_root
from coordinator_core.session.machinery_paths import plan_sidecars_dir as _plan_sidecars_dir
from coordinator_core.subagent_sandbox.provision_report import _exit_interview_section

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


def _derive_question_prompts() -> Tuple[str, ...]:
    """Derive the discovery-question prompts straight from
    ``provision_report._exit_interview_section()`` -- the single literal
    source of truth for the four questions -- rather than hand-duplicating
    them here. Each question renders as one ``- <question>`` bullet line
    (blank-line-separated, no embedded newlines within a question); this
    just strips the leading ``- `` marker off every non-blank line of the
    section body (skipping the ``## Exit interview`` heading itself).

    A hand-duplicated tuple here silently drifts from the rendered
    template on a one-sided reword (no test failure signals it) -- see
    commit f342615b75a2, which had to touch six files by hand to keep a
    single question wording in sync. Deriving instead of asserting makes
    that drift structurally impossible: there is exactly one literal.

    Fails LOUD on an empty derivation rather than degrading: with no
    prompts to strip, ``_is_answered`` finds the pristine question text
    still in the body and reports every untouched scaffold as ANSWERED --
    the same false-negative deriving exists to prevent, reintroduced one
    level down and silently. A section that renders no bullets means the
    template shape changed underneath this parser, which is a defect to
    surface at import, not to absorb.
    """
    section = _exit_interview_section()
    prompts: List[str] = []
    for line in section.splitlines():
        stripped = line.strip()
        if stripped.startswith("- "):
            prompts.append(stripped[2:].strip())
    if not prompts:
        raise RuntimeError(
            "exit-interview prompt derivation found no '- ' bullets in "
            "provision_report._exit_interview_section() -- the template "
            "shape changed; _is_answered cannot detect unanswered "
            "scaffolds until this parser is realigned"
        )
    return tuple(prompts)


#: The verbatim discovery-question prompts, derived from
#: ``provision_report._exit_interview_section()`` (the sidecar template's
#: single source of truth) rather than hand-duplicated -- see
#: ``_derive_question_prompts``'s docstring. A section is
#: "empty/unanswered" if, once these prompts (and blank lines/bullet
#: markers) are stripped out, nothing is left.
_QUESTION_PROMPTS = _derive_question_prompts()


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
    share_root = Path(_machinery_root(str(repo_root))) / "subagent-share"
    plan_sidecars_root = Path(_plan_sidecars_dir(str(repo_root)))
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
