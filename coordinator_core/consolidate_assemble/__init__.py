"""coordinator_core.consolidate_assemble — the `/consolidate-git` computed-
skill's read-only compute half.

Purpose: the dense mechanical spine `consolidate-git/SKILL.md` currently
hand-walks — current-identity resolution, branch enumeration + ownership
categorization (`git config user.email`, `git branch -a`,
`git log -1 --format=%ae`), worktree enumeration + reachability
categorization (`git worktree list --porcelain`, dirty check), unique-commit
computation (`git log cur..stale`), and inspection data-gather
(`git show --stat`) — computed ONCE here rather than re-derived by the EM
reading raw git output on every invocation. Returns the eight-key decision
object (`artifact`/`preflight`/`gates`/`directives`/`judgment_points`/
`decisions`/`narration`/`next_move`) per the Tier-B contract; every mutating
step is either an unconditional `directives[]` entry (a branch with zero
unique commits is safe to delete outright — no judgment needed) or gated
behind a `judgment_points[]` entry a human/EM disposition resolves
(absorb-or-skip on a branch WITH unique commits, dirty-worktree removal,
behind-main merge-first, and the Step 8 merge-ready recommendation).

`apply.py` (the mutating half) composes `coordinator_core.contract.apply_base`
directly — this module has no mutation opinion of its own; every directive
here names an existing atomic `cli` this package's own `apply.py` dispatch
table resolves.

Contract: example-doctrine-repo coordinator/docs/wiki/computed-skills.md
Spec backlink: docs/plans/2026-07-24-computed-skills-b4-baton-branch-lifecycle.md, chunk C8

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
    proc = run_git(["log", "-1", "--format=%ae", ref], repo_root)
    return proc.stdout.strip()


def categorize_branch(name: str, current: str, main_branch: Optional[str], tip_email: str, my_email: str) -> str:
    if name == current:
        return "current"
    if main_branch is not None and name == main_branch:
        return "main"
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


def worktree_is_dirty(run_git: RunGit, worktree_path: str) -> bool:
    proc = run_git(["--no-optional-locks", "status", "--porcelain"], Path(worktree_path))
    return bool(proc.stdout.strip())


def branch_reachable(run_git: RunGit, repo_root: Path, ref: str, target: str) -> bool:
    proc = run_git(["merge-base", "--is-ancestor", ref, target], repo_root)
    return proc.returncode == 0


def unique_commits(run_git: RunGit, repo_root: Path, current: str, stale_ref: str) -> list[str]:
    proc = run_git(["log", "--oneline", f"{current}..{stale_ref}"], repo_root)
    return [line for line in proc.stdout.splitlines() if line.strip()]


def inspect_commit(run_git: RunGit, repo_root: Path, sha: str) -> str:
    proc = run_git(["show", "--stat", sha], repo_root)
    return proc.stdout


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

    for entry in branch_entries:
        name, ref = entry["name"], entry["ref"]
        if name == current or (main_branch is not None and name == main_branch):
            branches_report.append({**entry, "category": "current" if name == current else "main"})
            continue

        author = tip_author(run_git, repo_root, ref)
        category = categorize_branch(name, current, main_branch, author, my_email)
        if category != "mine-stale":
            branches_report.append({**entry, "tip_author": author, "category": "others"})
            continue

        commits = unique_commits(run_git, repo_root, current, ref)
        branches_report.append(
            {**entry, "tip_author": author, "category": "mine-stale", "unique_commit_count": len(commits)}
        )

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

        inspections = [
            {"sha": line.split(" ", 1)[0], "stat": inspect_commit(run_git, repo_root, line.split(" ", 1)[0])}
            for line in commits
        ]
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
    for wt in worktree_entries:
        branch_name = wt.get("branch")
        wt_path = wt["path"]
        if Path(wt_path).resolve() == repo_root.resolve() or branch_name is None:
            worktrees_report.append({**wt, "category": "primary-or-detached"})
            continue
        if branch_name == current:
            worktrees_report.append({**wt, "category": "current"})
            continue

        author = tip_author(run_git, repo_root, branch_name if branch_name in {b["name"] for b in branch_entries} else wt_path)
        if author != my_email:
            worktrees_report.append({**wt, "tip_author": author, "category": "others"})
            continue

        reachable = branch_reachable(run_git, repo_root, branch_name, main_branch or current)
        dirty = worktree_is_dirty(run_git, wt_path)
        category = "stale-absorbed" if reachable else "stale-unique-work"
        worktrees_report.append(
            {**wt, "tip_author": author, "category": category, "dirty": dirty}
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
            judgment_points.append(
                build_judgment_point(
                    None,
                    id=jp_id,
                    question=f"Worktree {wt_path!r} has uncommitted changes — remove anyway?",
                    dispositions=[
                        build_disposition("proceed", resolves=[remove_directive_id]),
                        build_disposition("preserve"),
                    ],
                    evidence={"path": wt_path, "branch": branch_name},
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

    if any(d["cli"] == "worktree-remove" for d in directives):
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


def main(argv: list[str]) -> int:
    import json
    import sys

    if not argv:
        print("consolidate-assemble: usage: consolidate-assemble brief|apply [...]", file=sys.stderr)
        return EXIT_USAGE
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
        print(f"consolidate-assemble: transport failure: {exc}", file=sys.stderr)
        print(json.dumps({"error": str(exc), "transport_failure": True}))
        return EXIT_TRANSPORT_FAIL

    print(json.dumps(decision_object))
    return EXIT_OK
