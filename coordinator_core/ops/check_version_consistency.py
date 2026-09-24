"""
coordinator_core.ops.check_version_consistency — assert the coordinator-claude
version surfaces agree.

Purpose (RAG-bait): the coordinator-claude release has FOUR independent version
surfaces — the plugin manifest, the marketplace catalog manifest, the CHANGELOG,
and the git tag. They drifted apart historically (plugin.json 2.7.1 / marketplace
2.1.1 / CHANGELOG 2.8.1 on 2026-06-22) because the cut ceremony only stamped the
CHANGELOG and nothing asserted cross-surface agreement. This gate is that
assertion. It is the mechanical enforcer of docs/wiki/versioning-convention.md.

Invariant (steady-state, holds BETWEEN releases too):
  plugin.json .version
    == marketplace.json .metadata.version
    == latest *released* CHANGELOG `## [X.Y.Z]` section (Unreleased is skipped)
  [advisory, with --check-tag] == latest `v*` git tag

SSOT is plugin.json's version — every other surface tracks it. See the
convention doc for which number ships and how a bump moves all surfaces together.

Layout-agnostic: resolves every path RELATIVE TO marketplace.json, so it runs
unchanged in the meta-repo source layout (plugins/coordinator-claude/...) and in
the flat OSS publish-repo layout (repo root). Auto-discovers the bundle root.

Caller identity (P124-C1): with no `--root`, this gate no longer assumes its
caller HOLDS the coordinator-claude bundle just because a marketplace.json
happens to sit somewhere under it — example-retrieval-repo's own root marketplace.json
(name "example-retrieval-repo") is not the subject. `--repo-root <dir>` names the repo
being closed (default: the caller's cwd, via git toplevel, same as before);
every non-`--root` discovery rung is rooted at THAT value only, never cwd
directly, and accepts a candidate marketplace.json only when its `name` is
literally `"coordinator-claude"`. A repo whose root carries no such candidate,
or none whose name matches, is genuinely NOT the gate's subject: the gate
prints a stated not-applicable line and exits 0, rather than failing a caller
on a fact it cannot act on. This is a DELIBERATE departure from the retired
bash oracle, which exited 1 from every repo on the box (no caller ever exits 0
having "checked nothing" any more; see docs/decisions/ for the claude-klabauter DR this
implements). An explicit `--root` is unaffected: it stays fail-loud, exactly
as it always has, because the release callers (DoE's own ceremony step,
`publish.py`) depend on that behaviour.

Exit codes (parity-critical — the trampoline and callers branch on these):
  0 — all surfaces agree, or a stated not-applicable (no `--root`, no
      coordinator-claude bundle in `--repo-root`), or --help
  1 — mismatch, OR a required surface missing/unparseable (fail-loud)
  2 — unrecognised CLI argument

Port of: check-version-consistency.sh (DoE 894d4bc6, 2026-07-22)
Spec backlink: docs/wiki/versioning-convention.md (DoE-claude)
Port backlink: docs/plans/2026-07-16-bash-clean-slate-residual-migration.md
Caller-identity backlink: docs/plans/2026-09-12-ceremony-gates-read-their-caller-before-they-fail-it.md (C1)

Negative-spec:
    - `fail()` in the bash oracle always exits 1, even for "not found" /
      "unparseable" errors that read like usage mistakes — this module
      REPRODUCES that (does not "upgrade" those paths to exit 2), matching the
      oracle's own convention that only a genuinely unrecognised CLI flag is
      exit 2. This still holds for an explicit `--root`.
    - --check-tag is advisory ONLY: a tag/plugin.json mismatch prints a NOTE to
      stderr but never flips the exit code away from 0 (when surfaces
      otherwise agree). `--sort=-v:refname` failures (git <2.0) are silently
      swallowed exactly as the bash oracle's `2>/dev/null` does.
    - --quiet suppresses only the trailing "OK — all surfaces at X" line;
      failures always print regardless of --quiet, and so does the stated
      not-applicable line (matches oracle comment on failures; extended here
      to N/A on purpose — N/A is the fail-loud half of "checked nothing").
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import List, Optional

from coordinator_core.win_portability import no_console_creationflags
from coordinator_core.git.repo_root import show_toplevel

_PROG = "check-version-consistency"

_USAGE = """\
check-version-consistency.sh [--root <dir>] [--repo-root <dir>] [--check-tag] [--quiet]
  --root <dir>       bundle root containing .claude-plugin/marketplace.json
                      (fail-loud if missing; no identity check, no N/A branch)
  --repo-root <dir>  the repo being closed (default: cwd's git toplevel).
                      Ignored when --root is given. Auto-discovery is rooted
                      at this value only (never cwd) and only recognises a
                      marketplace.json whose "name" is "coordinator-claude" —
                      a repo with no such candidate exits 0 with a stated
                      not-applicable line, rather than failing.
  --check-tag        additionally compare the latest v* git tag (advisory: a
                      mismatch WARNs, does not fail — source_is_live repos
                      never tag)
  --quiet            suppress the OK line (failures and N/A always print)
"""

_VERSION_RE = re.compile(r'[ \t]*"version"[ \t]*:[ \t]*"([^"]*)".*')
_CHANGELOG_RE = re.compile(r'^## \[([0-9][0-9.]*)\].*')


class _GateFailure(Exception):
    """Internal control-flow signal for the bash oracle's fail()-then-exit-1 shape.

    The message is printed to stderr at the `_fail()` call site (matching the
    bash oracle's own `echo ... >&2; exit 1`, which prints immediately, not at
    unwind time) — this exception only carries control flow back to `main()`,
    which returns 1 without printing anything further.
    """


def _fail(msg: str) -> None:
    print(f"{_PROG}: {msg}", file=sys.stderr)
    raise _GateFailure(msg)


def _git_toplevel(start: Optional[str] = None) -> Optional[str]:
    out = show_toplevel(start or os.getcwd())
    return out or None


def _discover_root(root_arg: str) -> str:
    """The explicit `--root` branch ONLY (item 4: unchanged, fail-loud).

    NEGATIVE-SPEC (reproduced bug, not fixed): in the bash oracle,
    `BUNDLE_ROOT="$(discover_root)"` runs discover_root in a command-substitution
    SUBSHELL. `fail()`'s `exit 1` inside that subshell only terminates the
    subshell — it does NOT stop the parent script. The parent proceeds with
    BUNDLE_ROOT="" (only the subshell's stdout, which is empty, was captured;
    the fail() message went to stderr and printed anyway) and falls through to
    the CHANGELOG-file resolution using empty-root-relative paths, which always
    fails too, emitting a SECOND stderr message before the real top-level exit
    1. This module reproduces that two-message-then-continue sequence exactly
    for `--root`: it prints its own failure message immediately (matching bash
    echo timing) but returns "" instead of stopping the whole run, so the
    caller falls through to the CHANGELOG check exactly as the oracle does.

    The no-`--root` auto-discovery path is a DELIBERATE departure from the
    oracle (see the module docstring's Caller identity section) and lives in
    `_discover_bundle_for_repo_root`, never here — it never reproduces this
    subshell bug, because its failure mode is a stated not-applicable, not a
    second stderr message.
    """
    if not os.path.isfile(os.path.join(root_arg, ".claude-plugin", "marketplace.json")):
        print(
            f"{_PROG}: no .claude-plugin/marketplace.json under --root '{root_arg}'",
            file=sys.stderr,
        )
        return ""
    return root_arg


def _resolve_effective_repo_root(repo_root_arg: str) -> str:
    """The repo being closed: `--repo-root` if given, else cwd — normalized
    through `show_toplevel`, exactly as the pre-C1 cwd-derived rungs were, so
    a caller naming a path NESTED under a bundle holder's own worktree still
    resolves to that holder's root, and a caller naming a path that is not a
    git repo at all falls back to that path verbatim (discovered as N/A,
    naming the path the caller gave — never cwd)."""
    base = repo_root_arg or os.getcwd()
    top = _git_toplevel(base)
    return top or base


def _read_bundle_name(path: str) -> str:
    """Parses `path` (a marketplace.json candidate) as JSON and returns its
    top-level "name". Fails loud — exit 1, one stderr line naming the file —
    if the file exists but is not valid JSON or carries no string "name": a
    corrupt bundle-holder file must never be silently read as "not the
    bundle", which would turn a real defect into a green not-applicable."""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError) as exc:
        _fail(f"unparseable marketplace manifest: {path} ({exc})")
        raise AssertionError("unreachable")  # pragma: no cover
    name = data.get("name") if isinstance(data, dict) else None
    if not isinstance(name, str):
        _fail(f'unparseable marketplace manifest: {path} (no string "name")')
        raise AssertionError("unreachable")  # pragma: no cover
    return name


def _discover_bundle_for_repo_root(repo_root: str) -> Optional[str]:
    """Auto-discovery for the no-`--root` path. Three rungs, each rooted at
    `repo_root` ONLY — never cwd, never `_git_toplevel()` re-derived per rung
    — flat publish/source layout, DoE-claude's v3 source layout (the bundle
    lives under `coordinator/.claude-plugin/`), and the older nested
    meta-repo layout (`plugins/coordinator-claude/.claude-plugin/`). A
    candidate file that exists is identity-checked via `_read_bundle_name`
    before being accepted — a `example-retrieval-repo`-named marketplace.json is not
    mistaken for the coordinator-claude bundle, and a corrupt candidate fails
    loud rather than falling through. Returns the matching bundle root, or
    None when no rung has a coordinator-claude candidate at all (the
    not-applicable case, handled by the caller)."""
    for candidate in (
        f"{repo_root}/.claude-plugin/marketplace.json",
        f"{repo_root}/coordinator/.claude-plugin/marketplace.json",
        f"{repo_root}/plugins/coordinator-claude/.claude-plugin/marketplace.json",
    ):
        if not os.path.isfile(candidate):
            continue
        if _read_bundle_name(candidate) == "coordinator-claude":
            return candidate[: -len("/.claude-plugin/marketplace.json")]
    return None


def _resolve_plugin_json(bundle_root: str) -> str:
    """plugin.json sits beside marketplace.json in the flat publish/source
    layout (v3), or one level down under coordinator/ in the older nested
    meta-repo layout. Prefer the flat sibling; fall back to the nested path.

    Uses literal `f"{bundle_root}/..."` string concatenation, NOT
    `os.path.join`, to match the bash oracle's `"$BUNDLE_ROOT/..."` behaviour
    exactly: `os.path.join("", "x")` silently drops the empty leading segment
    and returns a CWD-relative path ("x"), whereas bash's string interpolation
    with an empty BUNDLE_ROOT produces an absolute-looking "/x" that (almost)
    never resolves. This distinction is load-bearing for the discover-root
    subshell-bug reproduction (see `_discover_root`'s docstring) — using
    os.path.join here would make an empty bundle_root silently succeed against
    an unrelated CWD-relative file instead of reproducing the oracle's failure.
    """
    flat = f"{bundle_root}/.claude-plugin/plugin.json"
    if os.path.isfile(flat):
        return flat
    return f"{bundle_root}/coordinator/.claude-plugin/plugin.json"


def _resolve_changelog(bundle_root: str) -> str:
    """CHANGELOG lives at the bundle root in the flat publish layout, under
    dist/publish-repo-toplevel/ in the flat v3 source layout, or under
    coordinator/dist/publish-repo-toplevel/ in the older nested meta-repo
    layout. Literal string concatenation — see `_resolve_plugin_json` docstring."""
    candidates = [
        f"{bundle_root}/CHANGELOG.md",
        f"{bundle_root}/dist/publish-repo-toplevel/CHANGELOG.md",
        f"{bundle_root}/coordinator/dist/publish-repo-toplevel/CHANGELOG.md",
    ]
    for c in candidates:
        if os.path.isfile(c):
            return c
    _fail(
        "no CHANGELOG.md found (looked at bundle root, dist/publish-repo-toplevel/, "
        "and coordinator/dist/publish-repo-toplevel/)"
    )
    raise AssertionError("unreachable")  # pragma: no cover


def _extract_json_version(path: str) -> str:
    """First `"version": "X.Y.Z"` line in the given file. plugin.json has exactly one."""
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            m = _VERSION_RE.match(line)
            if m:
                return m.group(1)
    return ""


def _extract_market_version(marketplace_path: str) -> str:
    """marketplace.json: the metadata.version specifically — NOT a plugin entry's.

    Plugin entries today carry no "version" key, but rather than bank on that
    schema invariant forever, scope the match to the metadata object by
    dropping everything from the "plugins": array onward before extracting.
    This stays correct if a plugin entry ever gains a version field.
    """
    plugins_key_re = re.compile(r'^[ \t]*"plugins"[ \t]*:')
    with open(marketplace_path, "r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            if plugins_key_re.match(line):
                break
            m = _VERSION_RE.match(line)
            if m:
                return m.group(1)
    return ""


def _extract_changelog_version(path: str) -> str:
    """CHANGELOG: first `## [X.Y.Z]` header that is NOT [Unreleased]."""
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            m = _CHANGELOG_RE.match(line)
            if m:
                return m.group(1)
    return ""


def _latest_v_tag(bundle_root: str) -> Optional[str]:
    """`--sort=-v:refname` requires git >=2.0; failures are silently skipped
    (advisory path — matches the oracle's `2>/dev/null`)."""
    try:
        res = subprocess.run(
            ["git", "-C", bundle_root, "tag", "--list", "v*", "--sort=-v:refname"],
            capture_output=True,
            text=True,
            **no_console_creationflags(),
        )
    except (OSError, FileNotFoundError):
        print(f"skip: _latest_v_tag: res = subprocess.run( failed: {sys.exc_info()[1]}", file=sys.stderr)
        return None
    if res.returncode != 0:
        return None
    lines = [ln for ln in res.stdout.splitlines() if ln.strip()]
    return lines[0] if lines else None


def _run(root_arg: str, repo_root_arg: str, check_tag: bool, quiet: bool) -> int:
    if root_arg:
        bundle_root = _discover_root(root_arg)
    else:
        repo_root = _resolve_effective_repo_root(repo_root_arg)
        discovered = _discover_bundle_for_repo_root(repo_root)
        if discovered is None:
            # Stated not-applicable — always printed, even under --quiet
            # (module docstring, Caller identity): a reader must be able to
            # tell "not-applicable" apart from "checked and green".
            print(
                f"{_PROG}: N/A — {repo_root} holds no coordinator-claude bundle "
                "(looked for .claude-plugin/ and coordinator/.claude-plugin/)"
            )
            return 0
        bundle_root = discovered

    marketplace = f"{bundle_root}/.claude-plugin/marketplace.json"
    plugin_json = _resolve_plugin_json(bundle_root)
    changelog = _resolve_changelog(bundle_root)

    if not os.path.isfile(plugin_json):
        _fail(f"missing plugin manifest: {plugin_json}")

    plugin_ver = _extract_json_version(plugin_json)
    if not plugin_ver:
        _fail(f"could not parse version from {plugin_json}")

    market_ver = _extract_market_version(marketplace)
    if not market_ver:
        _fail(f'could not parse metadata.version from {marketplace} (no "version" key before the "plugins" array)')

    changelog_ver = _extract_changelog_version(changelog)
    if not changelog_ver:
        _fail(f"no released '## [X.Y.Z]' section found in {changelog}")

    mismatch = plugin_ver != market_ver or plugin_ver != changelog_ver

    if mismatch:
        print("check-version-consistency: VERSION SURFACES DISAGREE", file=sys.stderr)
        print(f"  plugin.json      : {plugin_ver}   ({plugin_json})", file=sys.stderr)
        print(f"  marketplace.json : {market_ver}   ({marketplace})", file=sys.stderr)
        print(f"  CHANGELOG (latest): {changelog_ver}   ({changelog})", file=sys.stderr)
        print("", file=sys.stderr)
        print("  SSOT is plugin.json. Bump every surface together — see", file=sys.stderr)
        print("  docs/wiki/versioning-convention.md. At a release cut, stamping", file=sys.stderr)
        print("  CHANGELOG [Unreleased] -> [X.Y.Z] MUST also set plugin.json and", file=sys.stderr)
        print("  marketplace.json metadata.version to X.Y.Z in the same commit.", file=sys.stderr)
        return 1

    if check_tag:
        latest_tag = _latest_v_tag(bundle_root)
        if latest_tag:
            tag_ver = latest_tag[1:] if latest_tag.startswith("v") else latest_tag
            if tag_ver != plugin_ver:
                print(
                    f"check-version-consistency: NOTE — latest git tag {latest_tag} != "
                    f"plugin.json {plugin_ver} (advisory; expected if this release is not yet tagged)",
                    file=sys.stderr,
                )

    if not quiet:
        print(f"check-version-consistency: OK — all surfaces at {plugin_ver}")

    return 0


def main(argv: List[str]) -> int:
    """CLI entry: arg parse, run the gate, return exit code (the gate prints its
    own diagnostics as it goes, matching the bash oracle's immediate-echo timing)."""
    check_tag = False
    quiet = False
    root_arg = ""
    repo_root_arg = ""

    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg == "--root":
            root_arg = argv[i + 1] if i + 1 < len(argv) else ""
            i += 2
            continue
        if arg == "--repo-root":
            repo_root_arg = argv[i + 1] if i + 1 < len(argv) else ""
            i += 2
            continue
        if arg == "--check-tag":
            check_tag = True
            i += 1
            continue
        if arg == "--quiet":
            quiet = True
            i += 1
            continue
        if arg in ("-h", "--help"):
            print(_USAGE)
            return 0
        print(f"{_PROG}: unknown arg '{arg}'", file=sys.stderr)
        return 2

    try:
        return _run(root_arg, repo_root_arg, check_tag, quiet)
    except _GateFailure:
        print(
            f"skip: main: return _run(root_arg, repo_root_arg, check_tag, quiet) failed: {sys.exc_info()[1]}",
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
