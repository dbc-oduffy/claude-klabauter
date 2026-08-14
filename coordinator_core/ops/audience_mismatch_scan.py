"""
coordinator_core.ops.audience_mismatch_scan -- cadence-gate probe for recurring
doctrine-shaped gaps surfaced through the dispatched-agent exit interview.

Purpose: grep recent state/subagent-share/ run-report sidecars' "## Exit
interview" -> "What did you have to work out that the brief could have told
you?" answers for a recurring doctrine-shaped gap. The same rule missing from
multiple independent dispatch briefs is a mis-routing signal (a rule that
exists in doctrine but is not reaching the agents who need it) -- named but
not built by docs/plans/2026-07-27-claude-md-altitude-triage.md C7b
(coordinator/snippets/em-operating-doctrine.md, "MIS-ROUTING DETECTOR" note;
The Director of Engineering plan-review, major finding #6, half 2 of 2) and built here (C14). This
is NOT a new channel -- the exit-interview question this scans already
exists per em-operating-doctrine.md's assembled "Exit interview" section and
is enforced present-in-every-sidecar by
coordinator/tests/test_review_integrator_fill_guard.py -- this probe is a new
AGGREGATE READ over an existing answer field, wired into /workweek-complete
Step 5 as a cadence gate (coordinator/commands/workweek-complete.md).

Port style: modelled on coordinator_core.ops.check_harvest_debt -- a
read-only, argv-driven nudge probe invoked by name from a ceremony command
body, not a JSON-RPC op (no register_op; this is the same standalone-CLI
family as check_harvest_debt.py / check_weekly_staleness.py, not the
locked_rmw-mutating op family like workday_stitch_sidecar_summary.py).

Spec backlink: DoE-claude:pln-claude-md-altitude-triage-earn-31f32e, Tasks/C14
("the audience-mis-routing detector" named starter).

Matching strategy: near-duplicate clustering over the free-text
exit-interview answer, NOT literal-substring or embedding-based matching.
Each qualifying answer (see Negative-spec for what is excluded) is reduced to
a bag of "significant" tokens (lowercased, alnum-only, length >= 4, minus a
small stopword list) and greedily clustered with the first existing cluster
whose representative token-set has Jaccard similarity >= _SIMILARITY_THRESHOLD.
A cluster reaching >= _REPEAT_THRESHOLD distinct sidecars is a "recurring
doctrine-shaped gap" and is surfaced as a Step-5-shaped triage nudge.

Exit codes:
    0 -- probe completed (nudge printed or silent), OR state/subagent-share/
         is absent (consumer-project no-op -- nothing to scan). This probe
         never returns non-zero: it is purely advisory, matching the "seven
         advisory rows never block" posture of the Step 5 guard-sweep it
         feeds (coordinator/commands/workweek-complete.md § Computed-
         conversion manifest / Step 4b-4k guard-sweep census).

Negative-spec:
  - Does NOT treat "none" / "n/a" / "nothing notable" / "nothing" / empty
    answers as a doctrine-shaped gap -- these are the fill guard's OWN
    accepted "nothing to report" shape
    (test_review_integrator_fill_guard.py's FILLED_SIDECAR fixture uses
    "Nothing notable."), not a signal worth clustering.
  - Does NOT read every sidecar ever produced -- filtered to a recency
    window (default 14 days) via the sidecar's `spawned_at` frontmatter
    field (per the Run-Report Sidecar contract,
    coordinator/snippets/run-report-citizenship.md), falling back to file
    mtime only when frontmatter is absent or the field is unparseable
    (older or hand-authored sidecars).
  - Does NOT commit, mutate, or delete any sidecar -- read-only.
  - Does NOT auto-route the finding anywhere. It prints a triage-shaped
    nudge for the EM to action per the Step 5 dispatch pattern
    (coordinator/commands/workweek-complete.md Step 5), the same posture as
    the prior-art sidecar scan and initiative-govern sweep already wired
    into that step.
  - Does NOT attempt semantic/embedding similarity -- token-Jaccard
    clustering is a deliberately cheap, dependency-free heuristic; it will
    miss a paraphrase that shares no significant tokens. That is an accepted
    false-negative, not a bug to chase with heavier machinery here.
  - Does NOT let an EMPTY exit-interview answer (question asked, no text
    typed before the next bullet) bleed into the following question's text.
    The answer-capture regex uses same-line-only whitespace after the
    question mark (not the fully-greedy whitespace class) specifically so
    the blank-line separator in
    front of the next bullet still terminates an empty answer instead of
    being consumed as part of it -- an earlier draft of this probe got this
    wrong and reported the NEXT question's literal text as a "recurring
    doctrine-shaped gap" on a corpus of empty answers.
"""

from __future__ import annotations

import re
import subprocess
from coordinator_core.win_portability import no_console_creationflags
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from shutil import which

import yaml

_EXIT_INTERVIEW_QUESTION_RE = re.compile(
    r"-\s*What did you have to work out that the brief could have told you\?"
    # Same-line whitespace only here (NOT \s*) -- see negative-spec below: a
    # greedy \s* would also swallow the blank-line separator before the next
    # bullet on an EMPTY-answer sidecar, causing the lookahead to fire one
    # bullet too late and the "answer" to bleed into the next question.
    r"[ \t]*(.*?)(?=\n[ \t]*\n|\n-\s|\Z)",
    re.DOTALL,
)

_FRONTMATTER_RE = re.compile(r"\A---\n(.*?)\n---\n", re.DOTALL)

# Answers that mean "nothing to report" -- excluded from clustering entirely.
_NULL_ANSWER_RE = re.compile(
    r"^(none|n/?a|nothing( notable| in particular)?|no\.?)\.?$", re.IGNORECASE
)

_TOKEN_RE = re.compile(r"[a-z0-9]{4,}")
_STOPWORDS = frozenset(
    {
        "have", "that", "with", "this", "from", "were", "what", "there",
        "would", "could", "should", "which", "about", "brief", "notable",
        "wasnt", "didnt", "wasn", "didn", "into", "than", "then", "also",
        "only", "when", "where", "does", "doesn", "them", "they", "just",
    }
)

_DEFAULT_SINCE_DAYS = 14
_REPEAT_THRESHOLD = 3
_SIMILARITY_THRESHOLD = 0.5


def _resolve_root(explicit_root: str | None) -> str | None:
    if explicit_root:
        return explicit_root
    if which("git") is None:
        return None
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            check=False,
            **no_console_creationflags(),
        )
    except OSError:
        print(f"skip: _resolve_root: result = subprocess.run( failed: {sys.exc_info()[1]}", file=sys.stderr)
        return None
    if result.returncode != 0:
        return None
    top = result.stdout.strip()
    return top or None


def _parse_frontmatter(text: str) -> dict:
    match = _FRONTMATTER_RE.match(text)
    if not match:
        return {}
    try:
        data = yaml.safe_load(match.group(1))
    except yaml.YAMLError:
        return {}
    return data if isinstance(data, dict) else {}


def _sidecar_timestamp(path: Path, frontmatter: dict) -> datetime | None:
    """Resolve the sidecar's effective timestamp: `spawned_at` frontmatter
    field, falling back to file mtime when the field is absent or
    unparseable (see module docstring Negative-spec).

    PyYAML auto-parses an unquoted ISO-8601-shaped scalar into a native
    `datetime.datetime` (or `datetime.date`) rather than leaving it a `str`
    -- both shapes are handled here, not just the string form."""
    spawned_at = frontmatter.get("spawned_at")
    if isinstance(spawned_at, datetime):
        parsed = spawned_at
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed
    if isinstance(spawned_at, str):
        try:
            parsed = datetime.fromisoformat(spawned_at.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed
        except ValueError:
            pass
    try:
        return datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
    except OSError:
        return None


def _extract_exit_interview_answer(text: str) -> str | None:
    match = _EXIT_INTERVIEW_QUESTION_RE.search(text)
    if not match:
        return None
    answer = match.group(1).strip()
    return answer or None


def _is_null_answer(answer: str) -> bool:
    stripped = answer.strip().rstrip(".").strip()
    return bool(_NULL_ANSWER_RE.match(stripped))


def _significant_tokens(answer: str) -> set[str]:
    return {
        tok for tok in _TOKEN_RE.findall(answer.lower()) if tok not in _STOPWORDS
    }


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    intersection = len(a & b)
    union = len(a | b)
    return intersection / union if union else 0.0


class _Cluster:
    def __init__(self, sidecar_path: str, answer: str, tokens: set[str]) -> None:
        self.representative_answer = answer
        self.representative_tokens = tokens
        self.members: list[tuple[str, str]] = [(sidecar_path, answer)]

    def add(self, sidecar_path: str, answer: str) -> None:
        self.members.append((sidecar_path, answer))


def _cluster_answers(
    entries: list[tuple[str, str]],
) -> list[_Cluster]:
    """Greedily cluster (sidecar_path, answer) entries by token-Jaccard
    similarity against each cluster's representative (first-seen) answer.
    See module docstring "Matching strategy"."""
    clusters: list[_Cluster] = []
    for sidecar_path, answer in entries:
        tokens = _significant_tokens(answer)
        if not tokens:
            continue
        placed = False
        for cluster in clusters:
            if _jaccard(tokens, cluster.representative_tokens) >= _SIMILARITY_THRESHOLD:
                cluster.add(sidecar_path, answer)
                placed = True
                break
        if not placed:
            clusters.append(_Cluster(sidecar_path, answer, tokens))
    return clusters


def _collect_recent_answers(
    subagent_share_dir: Path, since: datetime
) -> list[tuple[str, str]]:
    entries: list[tuple[str, str]] = []
    for fpath in sorted(subagent_share_dir.rglob("*.md")):
        try:
            text = fpath.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        frontmatter = _parse_frontmatter(text)
        timestamp = _sidecar_timestamp(fpath, frontmatter)
        if timestamp is None or timestamp < since:
            continue
        answer = _extract_exit_interview_answer(text)
        if answer is None or _is_null_answer(answer):
            continue
        try:
            rel = str(fpath.relative_to(subagent_share_dir.parent.parent))
        except ValueError:
            rel = str(fpath)
        entries.append((rel, answer))
    return entries


def main(argv: list[str]) -> int:
    explicit_root: str | None = None
    since_days = _DEFAULT_SINCE_DAYS
    repeat_threshold = _REPEAT_THRESHOLD
    i = 0
    while i < len(argv):
        if argv[i] == "--root" and i + 1 < len(argv):
            explicit_root = argv[i + 1]
            i += 2
        elif argv[i] == "--since-days" and i + 1 < len(argv):
            since_days = int(argv[i + 1])
            i += 2
        elif argv[i] == "--repeat-threshold" and i + 1 < len(argv):
            repeat_threshold = int(argv[i + 1])
            i += 2
        else:
            i += 1

    root = _resolve_root(explicit_root)
    if not root:
        return 0

    root_path = Path(root)
    subagent_share_dir = root_path / "state" / "subagent-share"
    if not subagent_share_dir.is_dir():
        return 0

    since = datetime.now(tz=timezone.utc) - timedelta(days=since_days)
    entries = _collect_recent_answers(subagent_share_dir, since)
    if not entries:
        return 0

    clusters = _cluster_answers(entries)
    repeat_clusters = [c for c in clusters if len(c.members) >= repeat_threshold]
    if not repeat_clusters:
        return 0

    repeat_clusters.sort(key=lambda c: len(c.members), reverse=True)
    for cluster in repeat_clusters:
        count = len(cluster.members)
        example_paths = ", ".join(path for path, _ in cluster.members[:3])
        print(
            f"[audience-mismatch] {count} recent dispatched agents independently "
            f'had to work out the same doctrine-shaped gap: "{cluster.representative_answer}" '
            f"(e.g. {example_paths}) -- route back to the exit-interview channel's "
            "classification per em-operating-doctrine.md's MIS-ROUTING DETECTOR note."
        )

    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
