"""
coordinator_core.ops.release_tagging — idempotent annotated-tag cutting,
with an optional GitHub-release publish step (ops `release.cut_tag` and
`release.cut_tag_and_publish`).

Purpose: native replacement for the two release-tagging fences in
`coordinator/skills/merging-to-main/SKILL.md` (DoE-claude):

  - Mode A (`tag_anchor: git-tag`, SKILL.md:~481-497) — annotated-tag-only
    disclosure, no GitHub Release. Native op: `release.cut_tag`.
  - Mode B (default, SKILL.md:~514-530) — annotated tag PLUS un-draft/create
    the matching GitHub Release. Native op: `release.cut_tag_and_publish`.

Both fences share the same idempotent tag-cut core:

    git fetch origin main
    MERGE_SHA="$(git rev-parse origin/main)"
    existing="$(git rev-parse "$TAG" 2>/dev/null || true)"
    if [ "$existing" != "$MERGE_SHA" ]; then
        git tag -a "$TAG" "$MERGE_SHA" -m "$TAG"
        git push origin "$TAG"
    fi

This module ports that core natively (`_cut_tag`), then Mode B layers a
`gh release edit --draft=false --latest || gh release create ...` publish
step on top (`_publish_release`), sequenced strictly AFTER the tag push per
the fence's own prose ("release-edit failure must not leave the tag
un-pushed") and the C0a manifest's idempotency-hazard note for this row
("sequence the tag-push strictly before the release-publish call and
surface a release-call failure distinctly from a tag-push failure, so a
rerun after a partial failure does not re-attempt an already-succeeded tag
push").

Param-contract note on `tag_prefix` (read this before calling): the fence
resolves the *namespace* prefix (`""` or `"holodeck-"`) from
`coordinator.local.md` frontmatter and then hand-assembles the full tag as
`${TAG_PREFIX}vX.Y.Z`, where `X.Y.Z` is a PM-confirmed version number typed
in at release time — never itself parsed from anything on disk. The C0a
manifest's contract for both ops in this module carries exactly three
input keys (`repo_root`, `merge_sha`, `tag_prefix`) with no fourth
"version" key. Honoring that contract literally (this plan's manifest is
CONTRACTUAL — see plan § DEC-5), `tag_prefix` here is the FULLY-RESOLVED
tag name the caller wants cut (e.g. `"v0.9.0"` or `"holodeck-v0.4.0"`) —
the caller (the merging-to-main session, which already owns the
frontmatter-parse and the PM-confirmation step) is responsible for
concatenating its own resolved prefix with the confirmed version before
calling. This module does NOT parse `coordinator.local.md` and does NOT
prompt for or infer a version number; it is the purely mechanical
idempotent cut+push (+publish) step the fence's bash block performs once a
concrete tag string exists.

Idempotency (DEC-7 / AC7-AC8 note): both ops key their idempotency check on
`git rev-parse <tag>` vs `merge_sha`, exactly like the fence. A second
invocation with identical inputs finds the tag already at `merge_sha`
(`already_at_sha: true`, `created: false`, `pushed: false` — no git mutation
performed) and, for the publish op, still runs the release un-draft/create
step (itself idempotent via `gh release edit ... || gh release create ...`)
so a create-tag-but-publish-failed rerun completes the publish without
re-touching the tag. Manifest hazard rating for `cut-push-annotated-release-tag`
is `none` ("the fence's own spec is already idempotent... preserve that
existing-tag-sha guard natively"); for `cut-push-tag-and-publish-gh-release`
it is also `none`, contingent on the tag-then-release sequencing this
module enforces.

Windows-portability: `git` and `gh` are single cross-platform binaries
invoked as direct list-argv subprocesses (no shell interpreter in the
chain, no `shell=True`); console-window suppression via
`creationflags=CREATE_NO_WINDOW` (no-op on non-Windows), matching the
`create_github_remote.py` convention.

Negative-spec: neither op ever force-pushes or force-overwrites an existing
tag pointing at a DIFFERENT sha (no `-f` on `git tag` or `git push`) — a
genuine tag/sha collision raises rather than silently rewriting release
history, same as the fence's own un-guarded `git tag -a` would. Neither op
retries `gh` auth or installs `gh`; an absent/unauthenticated `gh` fails
loud with `gh`'s own stderr, never silently skipped.

Op contract (C0a manifest):
    release.cut_tag
    params: {repo_root: str, merge_sha: str, tag_prefix: str}
    -> {tag: str, created: bool, already_at_sha: bool, pushed: bool}

    release.cut_tag_and_publish
    params: {repo_root: str, merge_sha: str, tag_prefix: str, release_notes: str}
    -> {tag: str, tag_pushed: bool, release_created: bool, release_url: str | null}

    scope: common_dir (tag refs + the published release object are
    repo-wide, shared across every linked worktree of the same repo).

Spec backlink: pln-coordinator-ops-buildout-from--903224
Fence source: coordinator/skills/merging-to-main/SKILL.md:251 (DoE-claude, Mode A)
              coordinator/skills/merging-to-main/SKILL.md:290 (DoE-claude, Mode B)
"""

from __future__ import annotations

import asyncio
import subprocess
from coordinator_core.win_portability import no_console_creationflags
from pathlib import Path
from typing import Optional, Union

from coordinator_core.ipc import register_op

_PathLike = Union[str, Path, None]

_GIT_TIMEOUT = 60
# `git push` / `gh release` are network calls; cap generously but never hang forever.
_NETWORK_TIMEOUT = 180


def _run(
    cmd: list[str], cwd: _PathLike = None, timeout: int = _GIT_TIMEOUT
) -> subprocess.CompletedProcess:
    """Direct list-argv subprocess with hang cap + console suppression."""
    return subprocess.run(
        cmd,
        cwd=str(cwd) if cwd is not None else None,
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
        timeout=timeout,
        **no_console_creationflags(),
    )


def _git(args: list[str], cwd: _PathLike = None, timeout: int = _GIT_TIMEOUT) -> subprocess.CompletedProcess:
    return _run(["git", *args], cwd=cwd, timeout=timeout)


def _gh(args: list[str], cwd: _PathLike = None) -> subprocess.CompletedProcess:
    """All `gh` CLI traffic funnels through here — the test seam (mock gh)."""
    return _run(["gh", *args], cwd=cwd, timeout=_NETWORK_TIMEOUT)


def _existing_tag_sha(tag: str, repo_root: Path) -> Optional[str]:
    """Resolve *tag* to the COMMIT sha it points at.

    Dereferences via `<tag>^{commit}` rather than a bare `rev-parse <tag>`
    — for an annotated tag, `rev-parse <tag>` alone resolves to the tag
    OBJECT's own sha, not the commit sha the fence's `MERGE_SHA` comparison
    means; a bare rev-parse would spuriously fail the idempotency check on
    a second invocation and re-attempt (and fail on) `git tag -a`.
    """
    res = _git(["rev-parse", f"{tag}^{{commit}}"], cwd=repo_root)
    if res.returncode != 0:
        return None
    return res.stdout.strip() or None


def _validate_common(repo_root: Union[str, Path], merge_sha: str, tag: str) -> Path:
    if not tag:
        raise ValueError("release.cut_tag: `tag_prefix` (resolved tag name) is required")
    if tag.startswith("-"):
        # Review: code-reviewer (F5, nit) — tag is passed positionally to
        # several git subcommands (`tag -a`, `push origin`, `rev-parse`)
        # with no `--` separator; a value beginning with `-` would be
        # misparsed as a git flag rather than a ref/tag name.
        raise ValueError(
            f"release.cut_tag: tag {tag!r} looks like a git option (starts "
            "with '-'), not a tag name — refusing"
        )
    if not merge_sha:
        raise ValueError("release.cut_tag: `merge_sha` is required")
    root = Path(repo_root)
    if _git(["rev-parse", "--git-dir"], cwd=root).returncode != 0:
        raise ValueError(f"release.cut_tag: {root} is not a git worktree")
    return root


def _cut_tag(repo_root: Union[str, Path], merge_sha: str, tag: str) -> dict:
    """Idempotently cut + push an annotated tag *tag* at *merge_sha*.

    Mirrors the fence's own guard exactly: only creates/pushes when the tag
    does not already resolve to `merge_sha`. Never force-overwrites a tag
    pointing at a different sha (raises instead, same as the fence's
    un-guarded `git tag -a` would on a real collision).

    Returns {tag, created, already_at_sha, pushed}.
    """
    root = _validate_common(repo_root, merge_sha, tag)

    existing_sha = _existing_tag_sha(tag, root)
    if existing_sha == merge_sha:
        return {"tag": tag, "created": False, "already_at_sha": True, "pushed": False}

    tag_res = _git(["tag", "-a", tag, merge_sha, "-m", tag], cwd=root)
    if tag_res.returncode != 0:
        raise RuntimeError(
            f"release.cut_tag: git tag -a {tag!r} {merge_sha!r} failed: "
            f"{tag_res.stderr.strip()}"
        )

    push_res = _git(["push", "origin", tag], cwd=root, timeout=_NETWORK_TIMEOUT)
    if push_res.returncode != 0:
        raise RuntimeError(
            f"release.cut_tag: git push origin {tag!r} failed: {push_res.stderr.strip()}"
        )

    return {"tag": tag, "created": True, "already_at_sha": False, "pushed": True}


def _publish_release(tag: str, repo_root: Path, release_notes: str) -> tuple[bool, Optional[str]]:
    """Un-draft an existing release for *tag*, or create one if absent.

    Mirrors the fence's `gh release edit ... || gh release create ...`
    fallback chain. Returns (release_created, release_url); release_created
    is True only on the create path (edit-of-existing is not a "creation").
    Fails loud (RuntimeError) only if BOTH the edit and the create fail —
    an edit failure alone is expected whenever the release does not yet
    exist (the normal first-publish case).
    """
    notes_file = None
    try:
        edit_res = _gh(
            ["release", "edit", tag, "--draft=false", "--latest"], cwd=repo_root
        )
        if edit_res.returncode == 0:
            view = _gh(["release", "view", tag, "--json", "url"], cwd=repo_root)
            url = None
            if view.returncode == 0:
                import json as _json

                try:
                    url = _json.loads(view.stdout).get("url")
                except (ValueError, TypeError):
                    url = None
            return False, url

        import tempfile

        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".md", delete=False, encoding="utf-8"
        ) as fh:
            fh.write(release_notes or "")
            notes_file = fh.name

        create_res = _gh(
            [
                "release",
                "create",
                tag,
                "--latest",
                "--notes-file",
                notes_file,
            ],
            cwd=repo_root,
        )
        if create_res.returncode != 0:
            raise RuntimeError(
                f"release.cut_tag_and_publish: gh release create {tag!r} failed "
                f"(edit also failed: {edit_res.stderr.strip()}): "
                f"{create_res.stderr.strip()}"
            )
        url = create_res.stdout.strip() or None
        return True, url
    finally:
        if notes_file is not None:
            try:
                Path(notes_file).unlink(missing_ok=True)
            except OSError:
                pass


def cut_tag(repo_root: Union[str, Path], merge_sha: str, tag_prefix: str) -> dict:
    """Mode A (`tag_anchor: git-tag`): annotated-tag-only disclosure.

    See module docstring's "Param-contract note on `tag_prefix`" — the
    caller supplies the fully-resolved tag name via `tag_prefix`.
    """
    return _cut_tag(repo_root, merge_sha, tag_prefix)


def cut_tag_and_publish(
    repo_root: Union[str, Path],
    merge_sha: str,
    tag_prefix: str,
    release_notes: str,
) -> dict:
    """Mode B (default): annotated tag, sequenced strictly before the
    GitHub-release publish step (tag-push failure must never be masked by
    a subsequent release-publish attempt; a release-publish failure must
    never be conflated with a tag-push failure — C0a manifest hazard note).

    See module docstring's "Param-contract note on `tag_prefix`" — the
    caller supplies the fully-resolved tag name via `tag_prefix`.
    """
    tag = tag_prefix
    tag_result = _cut_tag(repo_root, merge_sha, tag)
    root = Path(repo_root)

    release_created, release_url = _publish_release(tag, root, release_notes)

    return {
        "tag": tag,
        "tag_pushed": tag_result["pushed"] or tag_result["already_at_sha"],
        "release_created": release_created,
        "release_url": release_url,
    }


@register_op("release.cut_tag")
async def _cut_tag_handler(params: dict, repo_root: Optional[Path] = None) -> dict:
    """JSON-RPC `release.cut_tag` handler.

    params: {repo_root: str, merge_sha: str, tag_prefix: str}
    -> {tag: str, created: bool, already_at_sha: bool, pushed: bool}

    `repo_root` in params is this op's contractual target (common_dir
    scope: tag refs are shared across every linked worktree); it takes
    precedence over the IPC-injected `repo_root`.
    """
    target = params.get("repo_root") or repo_root
    if target is None:
        raise ValueError("release.cut_tag: no repo_root (neither params nor IPC-injected)")
    merge_sha = params.get("merge_sha") or ""
    tag_prefix = params.get("tag_prefix") or ""
    return await asyncio.to_thread(cut_tag, target, merge_sha, tag_prefix)


@register_op("release.cut_tag_and_publish")
async def _cut_tag_and_publish_handler(params: dict, repo_root: Optional[Path] = None) -> dict:
    """JSON-RPC `release.cut_tag_and_publish` handler.

    params: {repo_root: str, merge_sha: str, tag_prefix: str, release_notes: str}
    -> {tag: str, tag_pushed: bool, release_created: bool, release_url: str | null}

    `repo_root` in params is this op's contractual target (common_dir
    scope); it takes precedence over the IPC-injected `repo_root`.
    """
    target = params.get("repo_root") or repo_root
    if target is None:
        raise ValueError(
            "release.cut_tag_and_publish: no repo_root (neither params nor IPC-injected)"
        )
    merge_sha = params.get("merge_sha") or ""
    tag_prefix = params.get("tag_prefix") or ""
    release_notes = params.get("release_notes") or ""
    return await asyncio.to_thread(
        cut_tag_and_publish, target, merge_sha, tag_prefix, release_notes
    )
