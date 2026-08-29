"""coordinator_core.consolidate_assemble — the `/consolidate-git` computed-
skill's read-only compute half.

Purpose: the dense mechanical spine `consolidate-git/SKILL.md` currently
hand-walks — current-identity resolution, branch enumeration + ownership
categorization (`git config user.email`, `git branch -a`,
`git log -1 --format=%ae`), worktree enumeration + reachability
categorization (`git worktree list --porcelain`, dirty check), unique-commit
computation (`git log cur..stale`), and inspection data-gather (one batched
`git show --stat <sha>...` across every stale branch's shas GLOBALLY, never
one spawn per branch and never one per commit — see `inspect_commits`) —
computed ONCE here rather than re-derived by the EM
reading raw git output on every invocation. Returns the eight-key decision
object (`artifact`/`preflight`/`gates`/`directives`/`judgment_points`/
`decisions`/`narration`/`next_move`) per the Tier-B contract; every mutating
step is either an unconditional `directives[]` entry (a branch with zero
unique commits is safe to delete outright — no judgment needed, EXCEPT a
backup ref, whose emptiness is its success condition; see
`is_backup_branch`) or gated
behind a `judgment_points[]` entry a human/EM disposition resolves
(absorb-or-skip on a branch WITH unique commits, dirty-worktree removal,
behind-main merge-first, and the Step 8 merge-ready recommendation).

`apply.py` (the mutating half) composes `coordinator_core.contract.apply_base`
directly — this module has no mutation opinion of its own; every directive
here names an existing atomic `cli` this package's own `apply.py` dispatch
table resolves.

Contract: DoE-claude coordinator/docs/wiki/computed-skills.md
Spec backlink: DoE-claude:pln-b4-baton-branch-lifecycle-comp-780d48, chunk C8

`brief()` routes every construction through the shipped
`coordinator_core.contract.decision_object.envelope.build_envelope` /
`.judgment.build_judgment_point`/`build_disposition` constructors — matching
`baton_assemble`'s and `orient_assemble`'s shape (Review: code-reviewer —
Finding 1; this module used to hand-assemble the 8-key envelope and each
`judgment_points[]` entry as a raw dict literal, bypassing `build_envelope`'s
own validation/defaults). Every emitted value (including each judgment
point's dict-shaped `evidence`) is unchanged from the prior hand-rolled
shape — a relocation through the canonical constructor, not a redesign.

Negative-spec:
    - Do NOT emit a verdict ("superseded", "safe to skip") for a branch
      carrying unique commits — that call is Step 4 evidence-gated judgment
      (`consolidate-git/SKILL.md` § Step 3 self-check); this module only
      ever emits the EVIDENCE (the commit list + `git show --stat` output)
      on the judgment point, never a pre-baked disposition.
    - Do NOT touch a branch/worktree whose tip-commit author is not
      `my_email` — such entries are reported in `gates.branches`/
      `gates.worktrees` with `category: "others"` and never appear in
      `directives[]` or `judgment_points[]`.
    - Do NOT re-introduce a per-commit OR per-branch `git show` — the
      inspection gather is one spawn total, across every stale branch's
      shas, not `unique_commits × ~100ms` or `stale_branches × ~100ms`
      (`coordinator_core/tests/test_no_unbatched_per_item_git_spawn.py`
      graduated this site off `_KNOWN_SITES`; a per-item or per-branch
      reintroduction fails that gate).
    - Do NOT shell out with a caller-derived string — every git invocation
      here is a literal argv list through the injected `run_git` callable,
      never a shell string built from a branch/path name.
    - Do NOT hand-roll a parallel 8-key envelope or judgment-point dict
      literal — route every construction through
      `decision_object.envelope.build_envelope` / `.judgment.
      build_judgment_point`/`build_disposition`, matching every sibling
      assembler in this baton.
"""
from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any, Callable, Optional

from coordinator_core.contract.decision_object.envelope import build_envelope
from coordinator_core.contract.decision_object.judgment import (
    build_disposition,
    build_judgment_point,
)

RunGit = Callable[[list[str], Path], "subprocess.CompletedProcess[str]"]

EXIT_OK = 0
EXIT_BUSINESS_FAILURE = 1
EXIT_USAGE = 2
EXIT_TRANSPORT_FAIL = 3

# Absorb-strategy threshold (consolidate-git/SKILL.md Step 4): 1-3 unique
# commits defaults to cherry-pick; more than that defaults to merge.
_CHERRY_PICK_MAX_COMMITS = 3


def default_run_git(args: list[str], cwd: Path) -> "subprocess.CompletedProcess[str]":
    from coordinator_core.win_portability import no_console_creationflags

    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=False,
        **no_console_creationflags(),
    )


def current_user_email(run_git: RunGit, repo_root: Path) -> str:
    proc = run_git(["config", "user.email"], repo_root)
    return proc.stdout.strip()


def current_branch(run_git: RunGit, repo_root: Path) -> str:
    proc = run_git(["rev-parse", "--abbrev-ref", "HEAD"], repo_root)
    return proc.stdout.strip()


def resolve_main_branch(run_git: RunGit, repo_root: Path) -> Optional[str]:
    for candidate in ("main", "master"):
        proc = run_git(["rev-parse", "--verify", "--quiet", candidate], repo_root)
        if proc.returncode == 0:
            return candidate
    return None


def list_branches(run_git: RunGit, repo_root: Path) -> list[dict[str, Any]]:
    """Parses `git branch -a` into `[{name, ref, is_local, is_remote}]`.
    `ref` is the git-log-resolvable reference for a remote-only branch
    (`origin/<name>`) or the bare local name otherwise. Skips the
    `remotes/origin/HEAD -> origin/main` alias line — it names no branch of
    its own."""
    proc = run_git(["branch", "-a"], repo_root)
    branches: dict[str, dict[str, Any]] = {}
    for raw_line in proc.stdout.splitlines():
        line = raw_line.strip().lstrip("* ").strip()
        if not line or "->" in line:
            continue
        if line.startswith("remotes/"):
            remote_name = line[len("remotes/"):]
            parts = remote_name.split("/", 1)
            if len(parts) != 2:
                continue
            name = parts[1]
            entry = branches.setdefault(name, {"name": name, "is_local": False, "is_remote": False})
            entry["is_remote"] = True
        else:
            name = line
            entry = branches.setdefault(name, {"name": name, "is_local": False, "is_remote": False})
            entry["is_local"] = True
    out = []
    for name, entry in branches.items():
        ref = name if entry["is_local"] else f"origin/{name}"
        out.append({"name": name, "ref": ref, "is_local": entry["is_local"], "is_remote": entry["is_remote"]})
    return out


def tip_author(run_git: RunGit, repo_root: Path, ref: str) -> str:
    """`git log -1 --format=%ae <ref>` resolves a single tip commit's
    author. Retained as the per-ref fallback `tip_authors` falls back to for
    a ref outside `refs/heads`/`refs/remotes` (e.g. a worktree path) that
    `git for-each-ref` cannot resolve."""
    proc = run_git(["log", "-1", "--format=%ae", ref], repo_root)
    return proc.stdout.strip()


def tip_authors(run_git: RunGit, repo_root: Path) -> dict[str, str]:
    """Batches `tip_author` across every local and remote-tracking ref into
    ONE `git for-each-ref` call: `%(refname:short)` yields `<name>` for a
    local branch and `origin/<name>` for a remote-only one — the same `ref`
    shape `list_branches` already produces — and `%(authoremail:trim)`
    strips the `<...>` `git log --format=%ae` never adds, so the returned
    map is keyed and valued identically to N per-ref `tip_author` calls."""
    proc = run_git(
        ["for-each-ref", "--format=%(refname:short) %(authoremail:trim)", "refs/heads", "refs/remotes"],
        repo_root,
    )
    authors: dict[str, str] = {}
    for raw_line in proc.stdout.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        name, _, email = line.partition(" ")
        authors[name] = email
    return authors


#: Branch-name segments that mark a ref as a deliberate safety copy. A backup
#: ref doing its job perfectly is byte-identical to what it protects, so it has
#: ZERO unique commits -- which the redundancy test below reads as "safe to
#: delete outright, no judgment needed" and turns into an unconditional delete
#: directive with no judgment point in front of it. That is exactly backwards:
#: emptiness is the backup's success condition, not evidence it is disposable.
#: Reported after a near-loss of unrecoverable history during an active
#: blob-purge (recovered via reflog).
#:
#: Deliberately broad, and deliberately erring toward retention: a working
#: branch wrongly spared costs one manual delete, a backup wrongly deleted can
#: cost history that exists nowhere else. Matched per slash-separated segment,
#: so `backup/pre-purge`, `wip/backup-2026-08-18`, and `pre-rewrite` all hold.
_BACKUP_BRANCH_SEGMENT_PREFIXES = ("backup", "pre-")


def is_backup_branch(name: str) -> bool:
    """True when any slash-separated segment of `name` marks a safety copy."""
    return any(
        segment.startswith(prefix)
        for segment in name.split("/")
        for prefix in _BACKUP_BRANCH_SEGMENT_PREFIXES
    )


def categorize_branch(name: str, current: str, main_branch: Optional[str], tip_email: str, my_email: str) -> str:
    if name == current:
        return "current"
    if main_branch is not None and name == main_branch:
        return "main"
    if is_backup_branch(name):
        return "backup"
    if tip_email == my_email:
        return "mine-stale"
    return "others"


def list_worktrees(run_git: RunGit, repo_root: Path) -> list[dict[str, Any]]:
    """Parses `git worktree list --porcelain` into
    `[{path, branch, head, locked}]`. `branch` is `None` for a detached
    worktree (never a candidate here)."""
    proc = run_git(["worktree", "list", "--porcelain"], repo_root)
    worktrees: list[dict[str, Any]] = []
    current: dict[str, Any] = {}
    for raw_line in proc.stdout.splitlines():
        line = raw_line.rstrip("\n")
        if not line:
            if current:
                worktrees.append(current)
                current = {}
            continue
        if line.startswith("worktree "):
            current = {"path": line[len("worktree "):], "branch": None, "head": None, "locked": False}
        elif line.startswith("HEAD "):
            current["head"] = line[len("HEAD "):]
        elif line.startswith("branch "):
            ref = line[len("branch "):]
            current["branch"] = ref[len("refs/heads/"):] if ref.startswith("refs/heads/") else ref
        elif line.startswith("locked"):
            current["locked"] = True
    if current:
        worktrees.append(current)
    return worktrees


def worktree_is_dirty(run_git: RunGit, worktree_path: str) -> dict[str, Any]:
    """Probes whether `worktree_path` has uncommitted changes via `git
    status --porcelain`. Returns DR-319's degraded-with-evidence posture —
    never fail-open (`docs/decisions/DR-319-session-fact-facade-shape-and-
    failure-posture.md`; reference shape, read-only:
    `coordinator_core/baton_assemble/__init__.py ::
    _compute_dirty_tree_attribution`). A bare `bool(proc.stdout.strip())`
    reads a failed call's empty stdout as "clean" (`returncode != 0` never
    inspected), which then authorizes worktree removal — the single most
    dangerous direction for this probe, since the caller uses "clean" to
    unlock a destructive op. A degraded read must be structurally
    distinguishable from a genuinely-clean one at the call site, never
    collapsed back to a bool before the caller decides.

    Returns:
      - computed: {"degraded": False, "value": <bool>}
      - degraded: {"degraded": True, "evidence": "<why the probe could not run>"}
    """
    proc = run_git(["--no-optional-locks", "status", "--porcelain"], Path(worktree_path))
    if proc.returncode != 0:
        return {
            "degraded": True,
            "evidence": (
                f"worktree dirty-tree probe: git status --porcelain exited "
                f"{proc.returncode} for {worktree_path!r} "
                f"(stderr: {proc.stderr.strip()!r}) — cannot tell dirty from clean."
            ),
        }
    return {"degraded": False, "value": bool(proc.stdout.strip())}


def branch_reachable(run_git: RunGit, repo_root: Path, ref: str, target: str) -> bool:
    proc = run_git(["merge-base", "--is-ancestor", ref, target], repo_root)
    return proc.returncode == 0


def branches_merged_into(run_git: RunGit, repo_root: Path, target: str) -> set[str]:
    """Batches `branch_reachable` across every local branch against ONE
    `target` into a single `git branch --merged <target>` call — the
    full-listing form `merge-base --is-ancestor` has no equivalent of,
    listing every local branch whose tip is reachable from `target`
    (ancestor-or-equal), the same relation `branch_reachable` tests
    per-ref. `target` is loop-invariant across the worktree loop's
    per-worktree `branch_reachable` calls (always `main_branch or
    current`), so one call here replaces one per worktree."""
    proc = run_git(["branch", "--merged", target], repo_root)
    names: set[str] = set()
    for raw_line in proc.stdout.splitlines():
        line = raw_line.strip().lstrip("* ").strip()
        if not line or "->" in line:
            continue
        names.add(line)
    return names


def unique_commits(run_git: RunGit, repo_root: Path, current: str, stale_ref: str) -> list[str]:
    """One call per stale branch by design: `git rev-list a..b c..d` computes
    `reachable({b, d}) \\ reachable({a, c})`, the union's exclusion against
    the union's reachable set, not a union of independent per-range
    subtractions. A naive multi-branch batch would silently misattribute
    commits across branches, so each `current..stale_ref` range stays its
    own invocation."""
    proc = run_git(["log", "--oneline", f"{current}..{stale_ref}"], repo_root)
    return [line for line in proc.stdout.splitlines() if line.strip()]


def inspect_commits(run_git: RunGit, repo_root: Path, shas: list[str]) -> dict[str, str]:
    """Returns `{sha: <that commit's `git show --stat` block>}` for every sha
    in `shas`, from ONE `git show` invocation rather than one per commit.

    `git show` accepts multiple revs and concatenates their blocks, each
    opening with a column-0 `commit <full-sha>` line; message bodies are
    indented four spaces in the medium format, so that line can only be a
    real block boundary. Blocks are matched against `shas` in order, so an
    abbreviated sha (what `unique_commits` yields) resolves against the full
    sha git prints. Per-commit bytes are unchanged from the per-commit call
    this replaces.

    A git spawn on Windows costs ~100ms (DoE memo
    `cross-repo/inbox/2026-08-08-doe-claude-em-engine-side-git-spawn-cost.md`,
    7-rep median), so a per-commit shape would cost `unique_commits ×
    ~100ms`, and a per-branch shape `stale_branches × ~100ms`, on every
    consolidation.

    Called exactly ONCE per `brief()` invocation, across the union of every
    stale branch's shas — not once per branch and not once per commit.
    `test_inspection_gather_is_one_show_spawn_for_all_branches` pins
    `len(show_calls) == 1` regardless of stale-branch count. Per-branch
    attribution of the result survives the collapse because a commit's own
    `git show --stat` block is a pure function of that commit — identical
    whether it rides alongside one branch's shas or three branches' shas in
    the same argv — so `brief()`'s caller keys the shared result dict by sha
    per branch rather than needing this function to know about branches at
    all. See `brief()`'s call site for the dedup-then-fan-out shape.
    """
    if not shas:
        return {}
    proc = run_git(["show", "--stat", *shas], repo_root)

    blocks: dict[str, str] = {}
    pending: Optional[str] = None
    lines: list[str] = []
    remaining = list(shas)
    for line in proc.stdout.splitlines(keepends=True):
        if remaining and line.startswith("commit ") and line[len("commit "):].startswith(remaining[0]):
            if pending is not None:
                blocks[pending] = _drop_batch_separator("".join(lines))
            pending = remaining.pop(0)
            lines = [line]
            continue
        if pending is not None:
            lines.append(line)
    if pending is not None:
        blocks[pending] = "".join(lines)
    return blocks


def _drop_batch_separator(block: str) -> str:
    """Drops the blank line `git show` emits BETWEEN shown objects, so a
    batched block is byte-identical to that commit's own `git show --stat`
    output. The final block has no separator and is never passed here."""
    return block[:-1] if block.endswith("\n\n") else block


def _cherry_pick_or_merge_cli(commit_count: int) -> str:
    return "cherry-pick-and-delete" if commit_count <= _CHERRY_PICK_MAX_COMMITS else "merge-and-delete"


def brief(
    repo_root: Optional[Path] = None,
    my_email: Optional[str] = None,
    run_git: Optional[RunGit] = None,
) -> dict[str, Any]:
    """Computes the eight-key decision object for `/consolidate-git`. Reads
    ONLY (no mutation) — every mutating action surfaces as a `directives[]`
    entry or a `judgment_points[]` entry a caller's `apply.py` resolves."""
    run_git = run_git or default_run_git
    repo_root = repo_root or Path.cwd()

    my_email = my_email or current_user_email(run_git, repo_root)
    current = current_branch(run_git, repo_root)
    main_branch = resolve_main_branch(run_git, repo_root)

    branch_entries = list_branches(run_git, repo_root)
    branches_report: list[dict[str, Any]] = []
    directives: list[dict[str, Any]] = []
    judgment_points: list[dict[str, Any]] = []

    # Two passes: gather every stale branch's unique-commit shas first, then
    # one GLOBAL `inspect_commits` call across the union of all of them
    # (see that function's docstring for why a commit's `git show --stat`
    # block is safe to key by sha alone, independent of which branch's loop
    # iteration it was collected from). This trades "one spawn per stale
    # branch" for "one spawn total" — see
    # `test_inspection_gather_is_one_show_spawn_for_all_branches` for the
    # pinned invariant and its attribution argument.
    stale_branches: list[dict[str, Any]] = []
    all_shas: list[str] = []

    # Hoisted out of the branch loop: one `for-each-ref` call resolves
    # every branch's tip author at once (see `tip_authors`'s docstring for
    # the key/value equivalence to the per-ref call it replaces).
    all_tip_authors = tip_authors(run_git, repo_root)

    for entry in branch_entries:
        name, ref = entry["name"], entry["ref"]
        if name == current or (main_branch is not None and name == main_branch):
            branches_report.append({**entry, "category": "current" if name == current else "main"})
            continue

        author = all_tip_authors[ref] if ref in all_tip_authors else tip_author(run_git, repo_root, ref)
        category = categorize_branch(name, current, main_branch, author, my_email)
        if category != "mine-stale":
            # Report the category actually computed. The old literal `"others"`
            # collapsed every non-stale branch into one bucket, which would have
            # hidden the `backup` category from the brief the moment it existed.
            branches_report.append({**entry, "tip_author": author, "category": category})
            continue

        commits = unique_commits(run_git, repo_root, current, ref)
        branches_report.append(
            {**entry, "tip_author": author, "category": "mine-stale", "unique_commit_count": len(commits)}
        )

        shas = [line.split(" ", 1)[0] for line in commits]
        stale_branches.append({"entry": entry, "name": name, "commits": commits, "shas": shas})
        all_shas.extend(shas)

    # Dedup preserving first-encounter order: the same sha can legitimately
    # be reachable from more than one stale branch (both diverged from
    # `current` and share a commit neither has in common with it) — that is
    # not an attribution hazard, because a commit's own `git show --stat`
    # block is identical regardless of which branch's argv it rides in on,
    # so every branch below just looks its own shas up in the one shared
    # result dict.
    global_stats = inspect_commits(run_git, repo_root, list(dict.fromkeys(all_shas)))

    for stale in stale_branches:
        entry, name, commits, shas = stale["entry"], stale["name"], stale["commits"], stale["shas"]
        ref = entry["ref"]

        delete_directive_id = f"d-delete-{name}"
        if not commits:
            directives.append(
                {
                    "id": delete_directive_id,
                    "cli": "delete-only",
                    "args": [name] + (["origin"] if entry["is_remote"] else []),
                    "depends_on": None,
                    "already_satisfied": False,
                }
            )
            continue

        inspections = [{"sha": sha, "stat": global_stats.get(sha, "")} for sha in shas]
        jp_id = f"j-absorb-{name}"
        absorb_directive_id = f"d-absorb-{name}"
        absorb_cli = _cherry_pick_or_merge_cli(len(commits))
        directives.append(
            {
                "id": absorb_directive_id,
                "cli": absorb_cli,
                "args": [name, ref] + (["origin"] if entry["is_remote"] else []),
                "depends_on": jp_id,
                "already_satisfied": False,
            }
        )
        directives.append(
            {
                "id": delete_directive_id,
                "cli": "delete-only",
                "args": [name] + (["origin"] if entry["is_remote"] else []),
                "depends_on": jp_id,
                "already_satisfied": False,
            }
        )
        judgment_points.append(
            build_judgment_point(
                None,
                id=jp_id,
                question=f"Branch {name!r} has {len(commits)} unique commit(s) — absorb or skip?",
                dispositions=[
                    build_disposition("absorb", resolves=[absorb_directive_id]),
                    build_disposition("skip", resolves=[delete_directive_id]),
                ],
                evidence={"commits": commits, "inspections": inspections},
                reason="insufficient-evidence",
                revalidate_at_dispatch=False,
            )
        )

    worktree_entries = list_worktrees(run_git, repo_root)
    worktrees_report: list[dict[str, Any]] = []
    branch_names_set = {b["name"] for b in branch_entries}
    # Hoisted out of the worktree loop: `target` is loop-invariant (always
    # `main_branch or current`), so one `git branch --merged` call replaces
    # one `branch_reachable` spawn per worktree (see
    # `branches_merged_into`'s docstring).
    merged_into_target = branches_merged_into(run_git, repo_root, main_branch or current)
    for wt in worktree_entries:
        branch_name = wt.get("branch")
        wt_path = wt["path"]
        if Path(wt_path).resolve() == repo_root.resolve() or branch_name is None:
            worktrees_report.append({**wt, "category": "primary-or-detached"})
            continue
        if branch_name == current:
            worktrees_report.append({**wt, "category": "current"})
            continue

        author = (
            all_tip_authors[branch_name]
            if branch_name in branch_names_set and branch_name in all_tip_authors
            else tip_author(run_git, repo_root, branch_name if branch_name in branch_names_set else wt_path)
        )
        if author != my_email:
            worktrees_report.append({**wt, "tip_author": author, "category": "others"})
            continue

        # `worktree_is_dirty` has no batch form at all: `git status
        # --porcelain` reads one working directory's state, and each
        # worktree is its own separate tree — no single git invocation
        # reports every worktree's dirty state at once.
        reachable = branch_name in merged_into_target
        dirty_probe = worktree_is_dirty(run_git, wt_path)
        # DR-319 posture, applied at the call site: a degraded probe is
        # never read as clean. This guard authorizes destructive worktree
        # removal, so "cannot tell" is treated as "do not proceed"
        # (fail-closed) — a degraded probe routes through the same
        # judgment-point-gated path as a genuinely dirty tree, never the
        # unconditional-removal path a false "clean" would unlock.
        dirty = dirty_probe["degraded"] or dirty_probe["value"]
        category = "stale-absorbed" if reachable else "stale-unique-work"
        worktrees_report.append(
            {
                **wt,
                "tip_author": author,
                "category": category,
                "dirty": dirty,
                "dirty_probe_degraded": dirty_probe["degraded"],
            }
        )

        if not reachable:
            # Unique work: goes through the same absorb-or-skip branch flow
            # above (the branch entry, if it exists, already carries a
            # judgment point) — the worktree itself is only removed once
            # that branch's disposition resolves the branch's own delete
            # directive; no separate worktree-removal directive here.
            continue

        remove_directive_id = f"d-worktree-remove-{wt_path}"
        if dirty:
            jp_id = f"j-worktree-dirty-{wt_path}"
            if dirty_probe["degraded"]:
                question = (
                    f"Worktree {wt_path!r}'s dirty-tree probe failed "
                    f"({dirty_probe['evidence']}) — cannot confirm clean, remove anyway?"
                )
            else:
                question = f"Worktree {wt_path!r} has uncommitted changes — remove anyway?"
            judgment_points.append(
                build_judgment_point(
                    None,
                    id=jp_id,
                    question=question,
                    dispositions=[
                        build_disposition("proceed", resolves=[remove_directive_id]),
                        build_disposition("preserve"),
                    ],
                    evidence={
                        "path": wt_path,
                        "branch": branch_name,
                        "probe_degraded": dirty_probe["degraded"],
                    },
                    reason="insufficient-evidence",
                    revalidate_at_dispatch=False,
                )
            )
            depends_on = jp_id
        else:
            depends_on = None
        directives.append(
            {
                "id": remove_directive_id,
                "cli": "worktree-remove",
                "args": [wt_path],
                "depends_on": depends_on,
                "already_satisfied": False,
            }
        )

    # C6 fix (docs/plans/2026-08-19-directives-name-an-op-not-a-cli.md § C6,
    # BREAK-CLASS): reads whichever key the directive actually carries
    # (`op` or `cli`) rather than unguardedly subscripting `d["cli"]`. Every
    # directive `_build_directives` emits today still carries `cli` (none of
    # consolidate's six verbs migrate — see `apply.py`'s own discriminator
    # comment above its `_CLI_DISPATCH`), so this is a latent-bug fix, not a
    # behavior change: the moment any consolidate verb carried `op` instead,
    # this would have raised `KeyError` here, before `apply()` and before
    # pre-validation.
    if any(d.get("op", d.get("cli")) == "worktree-remove" for d in directives):
        directives.append(
            {"id": "d-worktree-prune", "cli": "worktree-prune", "args": [], "depends_on": None, "already_satisfied": False}
        )
    if directives:
        directives.append(
            {"id": "d-fetch-prune", "cli": "fetch-prune", "args": [], "depends_on": None, "already_satisfied": False}
        )

    behind_main = None
    if main_branch is not None and current != main_branch:
        behind_main = not branch_reachable(run_git, repo_root, main_branch, current)
        if behind_main:
            judgment_points.append(
                build_judgment_point(
                    None,
                    id="j-behind-main",
                    question=f"{current!r} is behind {main_branch!r} — merge main in before absorbing?",
                    dispositions=[
                        build_disposition("merge-main-first"),
                        build_disposition("proceed-anyway"),
                    ],
                    evidence={"current": current, "main_branch": main_branch},
                    reason="insufficient-evidence",
                    revalidate_at_dispatch=False,
                )
            )

    if directives or judgment_points:
        judgment_points.append(
            build_judgment_point(
                None,
                id="j-merge-ready",
                question="Consolidation looks complete — chain into /merge-to-main?",
                dispositions=[
                    build_disposition("chain-to-merge"),
                    build_disposition("stop-here"),
                ],
                evidence={"branches": branches_report, "worktrees": worktrees_report},
                reason="insufficient-evidence",
                revalidate_at_dispatch=False,
            )
        )

    narration = (
        f"Ran ahead of you: inventoried {len(branches_report)} branch(es) and "
        f"{len(worktrees_report)} worktree(s); {len(directives)} mechanical directive(s) "
        f"computed, {len(judgment_points)} judgment point(s) remain."
    )

    return build_envelope(
        artifact={"path": str(repo_root), "classification": "repo", "frontmatter": {}, "resolution": None},
        preflight={"current_branch": current, "main_branch": main_branch, "my_email": my_email},
        gates={"branches": branches_report, "worktrees": worktrees_report, "behind_main": behind_main},
        directives=directives,
        judgment_points=judgment_points,
        decisions={},
        narration=narration,
        next_move=None if not judgment_points else "Resolve the open judgment point(s), then re-run apply.",
    )


_USAGE_TEXT = "consolidate-assemble: usage: consolidate-assemble brief|apply [...]"


def main(argv: list[str]) -> int:
    import json
    import sys

    if not argv:
        print(_USAGE_TEXT, file=sys.stderr)
        return EXIT_USAGE

    if argv[0] in ("--help", "-h"):
        print(_USAGE_TEXT)
        return EXIT_OK

    subcmd, rest = argv[0], argv[1:]

    if subcmd == "apply":
        from coordinator_core.consolidate_assemble.apply import main_apply

        return main_apply(rest)

    if subcmd != "brief":
        print(f"consolidate-assemble: unknown subcommand {subcmd!r}", file=sys.stderr)
        return EXIT_USAGE

    try:
        decision_object = brief()
    except Exception as exc:  # noqa: BLE001 - transport-failure backstop
        # Exit 3 means compute never ran: there is no decision object to
        # honestly emit, so the diagnostic goes to stderr and stdout stays
        # empty rather than carrying a fabricated or partial JSON object.
        print(f"consolidate-assemble: transport failure: {exc}", file=sys.stderr)
        return EXIT_TRANSPORT_FAIL

    print(json.dumps(decision_object))
    return EXIT_OK
