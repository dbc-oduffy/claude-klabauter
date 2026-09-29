"""coordinator_core.hooks.session_start_cloud_focus -- SessionStart(startup)
op: arm a cloud session's focus repo; report missing agent teams.

Port of: coordinator-content-repo `coordinator/hooks/scripts/session-start-cloud-focus.py`
(PM order: warm-hook migration, claude-klabauter slice). Same two facts, same
cloud-only gating, same silent-everywhere-else contract.

Cloud-only, by the engine's own detector (`coordinator_core.env_locality
.harness_rung`, harness rung only -- a headless VM is not a cloud session);
a silent no-op everywhere else. Two legs:

- Agent teams are on in every cloud session (the settings manifest's
  all-machines `CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=1`, applied at
  pre-boot). The harness reads that flag once, at process start, so this
  hook can only report it missing, never turn it on. Silent when it is `1`.
- `COORDINATOR_CLOUD_FOCUS_REPO`, `owner/repo` or bare `repo`. This hook
  locates that checkout, and gives its session branch an empty anchor
  commit when the branch carries nothing ahead of base (GitHub refuses a
  PR with no commits). It then points the session at
  `coordinator:cloud-channel`, which pushes, opens or reuses the draft PR,
  and subscribes to it. PR creation and subscription are MCP calls only the
  model can make, which is why this hook hands off rather than finishing
  the job -- the returned `additionalContext` line IS that handoff.

Shape change from the DoE source: env is read from `payload["env"]`, never
ambient `os.environ` (`coordinator_core.warm.caller_context`'s pattern --
this is a resident engine serving every session, not a process born for
one), and the anchor commit is written IN-PROCESS via this engine's own
git plumbing (`coordinator_core.git.commit`'s `_identity`/`_stamp`/
`_gpgsign_enabled`/`_sign_commit_tree`/`_cas_target` plus `git_objects
.write_object`/`cas_ref`) instead of `git commit-tree`/`git update-ref`
subprocesses -- zero NEW spawns beyond the one `commit.py` already pays
when `commit.gpgsign` is on. `base_branch`'s origin-HEAD/main/master
resolution and the ahead-of-base check are likewise ported to zero-spawn
reads (`git_objects._ref_exists_loose_or_packed`, `git.commit_walk.walk`)
rather than `git symbolic-ref`/`git for-each-ref`/`git rev-list --count`.

Contract, unchanged: never blocks, never raises past the handler; every
failure degrades to silence or to a line naming what is missing.

Spec backlink: docs/research/spike-verdicts/2026-08-31-adapter-shape-for-
warm-hook-migration.md
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Iterable, List, Mapping, Optional, Tuple

from coordinator_core._hook_envelope import payload_of
from coordinator_core.hooks._envelope import context_only, no_advisory
from coordinator_core.ipc import register_op

FOCUS_ENV = "COORDINATOR_CLOUD_FOCUS_REPO"
TEAMS_FLAG_ENV = "CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS"
SESSION_ID_ENV = "CLAUDE_CODE_REMOTE_SESSION_ID"

# abs-path-ok: cloud-sandbox mount points, not a machine-local repo path -- ported
# verbatim from the DoE source script's own `CHECKOUT_ROOTS`.
CHECKOUT_ROOTS = (Path("/home/user"), Path("/workspace"))

_REMOTE_SLUG = re.compile(r"[/:]([^/:]+)/([^/]+?)(?:\.git)?/?$")


def parse_focus(value: str) -> Tuple[Optional[str], str]:
    value = value.strip().strip("/")
    if value.lower().endswith(".git"):
        value = value[:-4]
    if "/" in value:
        owner, _, repo = value.rpartition("/")
        return owner.rpartition("/")[2] or None, repo
    return None, value


def origin_slug(checkout: Path) -> Optional[Tuple[str, str]]:
    try:
        text = (checkout / ".git" / "config").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    in_origin = False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("["):
            in_origin = stripped.replace(" ", "") == '[remote"origin"]'
            continue
        if in_origin and stripped.startswith("url"):
            match = _REMOTE_SLUG.search(stripped.partition("=")[2].strip())
            return (match.group(1), match.group(2)) if match else None
    return None


def _candidates(roots: Iterable[Path]) -> List[Path]:
    seen: List[Path] = []
    for root in roots:
        try:
            children = sorted(p for p in root.iterdir() if p.is_dir())
        except OSError:
            children = []
        for path in [root, *children]:
            if path not in seen and (path / ".git").is_dir():
                seen.append(path)
    return seen


def find_checkout(owner: Optional[str], repo: str,
                  roots: Iterable[Path]) -> Optional[Tuple[Path, str]]:
    for path in _candidates(roots):
        slug = origin_slug(path)
        if slug is None:
            continue
        if slug[1].lower() == repo.lower() and (owner is None or slug[0].lower() == owner.lower()):
            return path, f"{slug[0]}/{slug[1]}"
    return None


def read_branch(checkout: Path) -> Optional[str]:
    try:
        head = (checkout / ".git" / "HEAD").read_text(encoding="utf-8").strip()
    except OSError:
        return None
    prefix = "ref: refs/heads/"
    return head[len(prefix):] if head.startswith(prefix) else None


def _ref_sha(common_dir: Path, ref: str) -> Optional[str]:
    """Zero-spawn `git rev-parse <ref>`-equivalent: a loose ref file, else a
    `packed-refs` line. Mirrors `git_state.head_sha`'s own fallback rung."""
    try:
        sha = (common_dir / ref).read_text(encoding="utf-8").strip()
        if sha:
            return sha
    except OSError:
        pass
    try:
        packed_text = (common_dir / "packed-refs").read_text(encoding="utf-8")
    except OSError:
        return None
    for line in packed_text.splitlines():
        if not line or line[0] in "#^":
            continue
        sha, _, ref_name = line.partition(" ")
        if ref_name == ref:
            return sha
    return None


def base_branch(checkout: Path) -> Optional[str]:
    """Zero-spawn equivalent of the source script's `git symbolic-ref` +
    `git for-each-ref` pair: reads `refs/remotes/origin/HEAD`'s symref
    target directly, falling back to checking `main` then `master` for
    existence via the same loose-or-packed reader `commit.py`'s own CAS
    target resolution already relies on."""
    from coordinator_core.git.git_dir import resolve_git_common_dir
    from coordinator_core.git.git_objects import _ref_exists_loose_or_packed

    common_dir = resolve_git_common_dir(checkout)
    try:
        head_text = (
            common_dir / "refs" / "remotes" / "origin" / "HEAD"
        ).read_text(encoding="utf-8").strip()
    except OSError:
        head_text = ""
    prefix = "ref: refs/remotes/origin/"
    if head_text.startswith(prefix):
        return head_text[len(prefix):].strip() or None
    for name in ("main", "master"):
        if _ref_exists_loose_or_packed(common_dir, f"refs/remotes/origin/{name}"):
            return name
    return None


def _ahead_of_base(common_dir: Path, head: str, base_sha: str) -> bool:
    """True iff `head` carries a commit `base_sha` cannot reach -- the
    zero-spawn equivalent of `git rev-list --count origin/<base>..HEAD` !=
    "0", walked via `commit_walk.walk` (already in this engine, no new
    spawn) rather than drained via a subprocess."""
    if head == base_sha:
        return False
    from coordinator_core.git import commit_walk

    # Ahead unless HEAD is an ancestor of base (a branch behind base carries nothing).
    for sha, _commit in commit_walk.walk(common_dir, base_sha):
        if sha == head:
            return False
    return True


def write_anchor_commit(repo: Path, message: str) -> bool:
    """Land an empty anchor commit reusing HEAD's own tree -- in-process,
    via the SAME primitives `coordinator_core.git.commit.commit_paths` uses
    for its own commit-object write and signed-commit leg, minus the
    parts that do not apply to a zero-path, tree-reused commit (no pathspec
    to assemble, no rollback check, no index splice -- nothing staged or
    on-disk changes). Returns False on any failure; never raises past this
    function's own try/except in `ensure_anchor`."""
    from coordinator_core.git.commit import (
        _cas_target,
        _gpgsign_enabled,
        _identity,
        _sign_commit_tree,
        _stamp,
    )
    from coordinator_core.git.git_dir import resolve_git_dir
    from coordinator_core.git.git_objects import cas_ref, write_object
    from coordinator_core.git.git_state import head_sha, head_tree_sha

    old_head = head_sha(repo)
    root_tree = head_tree_sha(repo)
    if not old_head or not root_tree:
        return False

    name, email = _identity(repo)
    when = _stamp()
    who = f"{name} <{email}> {when}"
    body = f"tree {root_tree}\nparent {old_head}\nauthor {who}\ncommitter {who}\n\n{message}"
    if not body.endswith("\n"):
        body += "\n"

    commit_sha: Optional[str] = None
    if _gpgsign_enabled(repo):
        # The one conditional spawn this route pays -- same gate `commit_
        # paths` uses, and only when `commit.gpgsign` already reads true.
        commit_sha, _warning = _sign_commit_tree(
            repo, root_tree, old_head, name, email, when, message,
        )
    gitdir = resolve_git_dir(repo)
    if commit_sha is None:
        commit_sha = write_object(gitdir, b"commit", body.encode("utf-8", "surrogateescape"))

    target = _cas_target(repo)
    if target is None:
        return False
    ref_gitdir, ref = target
    return cas_ref(
        ref_gitdir, ref, old_head, commit_sha,
        reflog_committer=f"{name} <{email}>",
        reflog_message=message.splitlines()[0] if message.strip() else "commit",
        head_gitdir=gitdir,
    )


def ensure_anchor(checkout: Path, branch: str, base: str, session_id: str) -> bool:
    from coordinator_core.git.git_dir import resolve_git_common_dir
    from coordinator_core.git.git_state import head_sha

    common_dir = resolve_git_common_dir(checkout)
    base_sha = _ref_sha(common_dir, f"refs/remotes/origin/{base}")
    head = head_sha(checkout)
    if not head or not base_sha:
        return False
    if _ahead_of_base(common_dir, head, base_sha):
        return True
    message = (
        "cloud: open session channel\n\n"
        "Empty anchor so this branch can carry the session's draft PR "
        "(coordinator:cloud-channel).\n\n"
        f"Cloud-Session-Id: {session_id or 'unknown'}\n"
    )
    try:
        return write_anchor_commit(checkout, message)
    except Exception:  # noqa: BLE001 -- SessionStart never blocks
        return False


def render_teams_line(flag: str) -> Optional[str]:
    if flag == "1":
        return None
    return (
        f"CLOUD: agent teams OFF. Add {TEAMS_FLAG_ENV}=1 to the env-var box, then start a "
        "new session."
    )


def render_focus_line(focus: str, name: Optional[str], branch: Optional[str],
                      base: Optional[str], ready: bool) -> str:
    if name is None:
        return f"CLOUD FOCUS: no checkout matches {focus}; select that repo for this environment."
    if branch is None or (base is not None and branch == base):
        where = "a detached HEAD" if branch is None else branch
        return f"CLOUD FOCUS: {name} is on {where}; cut a branch, then run coordinator:cloud-channel."
    if not ready:
        return f"CLOUD FOCUS: {name} {branch} is level with base; commit, then run coordinator:cloud-channel."
    return f"CLOUD FOCUS: {name}, branch {branch} -> {base}. First act: invoke coordinator:cloud-channel."


def is_cloud_session(env: Mapping[str, str]) -> bool:
    from coordinator_core.env_locality import harness_rung

    hit = harness_rung(env)
    return hit is not None and hit.call == "cloud"


def compute_context(payload: dict) -> Optional[str]:
    env = payload.get("env")
    if not isinstance(env, Mapping):
        env = {}

    try:
        if not is_cloud_session(env):
            return None
    except Exception:  # noqa: BLE001
        return None

    cwd = payload.get("cwd")
    session_id = str(env.get(SESSION_ID_ENV, "") or "")
    focus = str(env.get(FOCUS_ENV, "") or "").strip()

    lines: List[Optional[str]] = []
    try:
        lines.append(render_teams_line(str(env.get(TEAMS_FLAG_ENV, "") or "").strip()))
        if focus:
            owner, repo = parse_focus(focus)
            roots: List[Path] = [*CHECKOUT_ROOTS]
            if isinstance(cwd, str) and cwd:
                roots.append(Path(cwd))
            found = find_checkout(owner, repo, roots) if repo else None
            checkout, name = found if found else (None, None)
            branch = read_branch(checkout) if checkout else None
            base = base_branch(checkout) if checkout else None
            ready = base is not None
            if checkout and branch and base and branch != base:
                ready = ensure_anchor(checkout, branch, base, session_id)
            lines.append(render_focus_line(focus, name, branch, base, ready))
    except Exception:  # noqa: BLE001
        pass

    context = "\n".join(line for line in lines if line)
    return context or None


@register_op("hooks.session_start_cloud_focus")
def _handler(params: dict, repo_root=None) -> dict:
    params = payload_of(params)
    try:
        additional_context = compute_context(params)
    except Exception:  # noqa: BLE001 -- SessionStart never blocks
        additional_context = None

    if not additional_context:
        return no_advisory()
    return context_only("SessionStart", additional_context)
