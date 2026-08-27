"""Section porter — CoordinatorRoots (envelope key: ``coordinator_roots``).

Emits the coordinator-roots array. Post-2026-07-07 per-repo-emission-cutover, each
snapshot's ``coordinator_roots`` is ``[R]`` — a self-report only: element ``[0]`` is the
emitting repo R itself. Fleet-census role (enumerating all repos via ``working-repos.yaml``)
stays with the meta-repo ``~/.claude`` (the one that owns ``working-repos.yaml``);
non-meta-repo snapshots carry only their own self-report entry. ``board_member: false``
opts a repo out of fleet iteration (relevant only for the meta-repo snapshot).

PM ruling 2026-07-29 — GitHub API call removed. This section previously shelled out to
``gh repo view <slug> --json visibility,isArchived,isFork,defaultBranchRef,pushedAt`` per
coordinator root (parity with bash:1311/1428, carried forward faithfully during the
bash-to-Python port). That is a live remote call on claude-klabauter's emission path, which does not
belong here — claude-klabauter emits authoritative disk-truth; GitHub repo metadata is remote state,
and the fleet query surface belongs to cockpit (see project CLAUDE.md § Project Overview).
It was also an output-determinism defect: on any ``gh`` failure the code already degraded to
the local_fs fallback below, so the emitted record already differed by network availability.
The call is now removed outright — the local-filesystem path (previously only a failure
fallback) is unconditional, for both the self-report and the fleet entries. ``visibility``,
``archived``, ``is_fork``, ``default_branch``, and ``last_activity_at`` now always carry the
local_fs defaults (see ``_local_fs_record``); ``provenance.source_kind`` is always
``"local_fs"``. The malformed bucket is retained for schema shape but is now always empty —
there is no more per-repo remote-failure mode to populate it.

The bash oracle fans the fleet ``gh`` calls out concurrently (Phase 1 ``&``/``wait``) then
assembles in YAML order (Phase 2); this port issues them sequentially in the same order —
the emitted array ordering is identical, and the concurrency was a latency optimization,
not a semantic (now moot — there is no remote call left to fan out). Emit-DERIVED fields are
not applicable to this entity.

Port of: emit-cockpit-snapshot.sh (DoE 07eedcfb, 2026-07-19) — § SECTION 7,
  CoordinatorRoots. Byte/semantic parity port (parity with the gh-backed path retired
  2026-07-29 per the PM ruling above).
Spec backlink: pln-tc-3-emission-stack-python-por-c9595b § P07

Consumer deduplication contract (S3-F7 deferral — NOT a claude-klabauter fix; cockpit's responsibility):
Post-cutover, cockpit receives coordinator_roots entries for claude-klabauter from TWO sources:
(1) the meta-repo's fleet census (working-repos.yaml walk), and (2) claude-klabauter's own
self-report (element [0] of its own snapshot). Cockpit MUST deduplicate coordinator_roots by
``repo`` slug across all ingested snapshots (latest-wins / self-report-authority). Failure to
deduplicate would cause claude-klabauter to appear twice in the fleet-census view. This is a
tracked follow-up on cockpit's side (self-report-authority model). Claude-klabauter emits correctly per
the plan; the deduplication contract lives at the consumer layer.
"""

from __future__ import annotations

import re
from pathlib import Path

from coordinator_core.ops.emit.context import EmitContext
from coordinator_core.ops.emit.sections._shared import run_git

# ``- path:`` list-item detector for the working-repos.yaml walk (bash awk :1406).
_DASH_PATH_RE = re.compile(r"^[ \t]*-[ \t]+path:")


def _local_fs_provenance(ctx: EmitContext, repo: str) -> dict:
    """Build the git-only ``local_fs`` provenance envelope (bash:1374-1381, ref null / D9)."""
    return {
        "source_kind": "local_fs",
        "repo": repo,
        "ref": None,
        "path": "",
        "observed_at": ctx.observed_at,
        "derivation": "parsed",
        "entity_anchor": None,
    }


def _local_fs_record(ctx: EmitContext, repo: str, last_activity_at: str) -> dict:
    """Assemble one CoordinatorRoot record from local-filesystem data only (local_fs).

    Review: code-reviewer — Finding 6 (2026-07-14 entity_anchor slice review) — named
    helper so the record shape can't drift across call sites. Now the section's ONLY
    record constructor (PM ruling 2026-07-29 retired the ``gh``-backed sibling).
    """
    return {
        "repo": repo,
        "owner": repo.split("/")[0],
        "coordinator_root_path": ".",
        "machine": ctx.hostname,
        "visibility": "PRIVATE",
        "archived": False,
        "is_fork": False,
        "default_branch": "main",
        "last_activity_at": last_activity_at,
        "open_pr_count": None,
        "provenance": _local_fs_provenance(ctx, repo),
    }


def _parse_working_repos(path: Path) -> list[tuple[str, str]]:
    """Parse ``working-repos.yaml`` into an ordered ``[(slug, board_member), ...]`` list.

    Faithful port of the bash awk state machine (bash:1402-1410): only the ``repos:`` block
    is walked (terminated by ``out_of_tree:`` or EOF); each ``- path:`` list item flushes the
    previous entry; ``github:`` sets the slug, ``board_member:`` the opt-out flag; both values
    are stripped of the key prefix, trailing ``#`` comments, and surrounding whitespace. File
    order is preserved (the emitted fleet ordering depends on it).
    """
    results: list[tuple[str, str]] = []
    found = False
    slug = ""
    board_member = ""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return results

    def _strip_value(line: str, key: str) -> str:
        val = re.sub(rf"^[ \t]*{re.escape(key)}[ \t]*", "", line)
        val = re.sub(r"#.*$", "", val)
        val = re.sub(r"[ \t]*$", "", val)
        return val

    for raw in text.splitlines():
        line = raw.replace("\r", "")
        if re.match(r"^repos:", line):
            found = True
            continue
        if re.match(r"^out_of_tree:", line):
            if found and slug != "":
                results.append((slug, board_member))
            found = False
            slug = ""
            board_member = ""
            continue
        if found and _DASH_PATH_RE.match(line):
            if slug != "":
                results.append((slug, board_member))
            slug = ""
            board_member = ""
            continue
        if found and "github:" in line:
            slug = _strip_value(line, "github:")
            continue
        if found and "board_member:" in line:
            board_member = _strip_value(line, "board_member:")
            continue
    if slug != "":
        results.append((slug, board_member))
    return results


def collect(ctx: EmitContext) -> tuple[list[dict], list[dict]]:
    """Build the CoordinatorRoots array (element [0] = emitting repo R, then the fleet when working-repos.yaml is present).

    Review: code-reviewer (S3-F2) — stale "local meta-repo" attribution corrected. Post-cutover,
    element [0] is the EMITTING repo (claude-klabauter when claude-klabauter is emitting, ~/.claude
    when ~/.claude is emitting). The old phrasing was only accurate for meta-repo snapshots.

    PM ruling 2026-07-29 — every record is now local_fs (no ``gh`` call); ``malformed`` stays
    part of the return shape for schema/envelope compatibility but is always empty — there is
    no remaining per-repo failure mode to populate it.
    """
    records: list[dict] = []
    malformed: list[dict] = []

    # ---- Element [0]: local meta-repo — MANDATORY (bash:1307-1385) ------------------
    local_repo = ctx.repo_name
    local_last_activity = run_git(ctx.repo_root, "log", "-1", "--format=%cI", "HEAD") or ctx.observed_at
    records.append(_local_fs_record(ctx, local_repo, local_last_activity))

    # ---- Fleet iteration: working-repos.yaml in file order (bash:1387-1509) ---------
    working_repos_yaml = ctx.repo_root / "working-repos.yaml"
    if working_repos_yaml.is_file():
        for slug, board_member in _parse_working_repos(working_repos_yaml):
            if not slug:
                continue
            if board_member == "false":  # deliberate opt-out — not a failure
                continue
            records.append(_local_fs_record(ctx, slug, ctx.observed_at))

    return records, malformed
