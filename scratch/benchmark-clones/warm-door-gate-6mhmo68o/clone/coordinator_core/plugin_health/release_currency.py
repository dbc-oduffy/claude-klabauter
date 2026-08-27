"""
coordinator_core.plugin_health.release_currency — native release-currency probe.

Purpose: resolve the installed coordinator plugin version (via `version.txt`'s 40-hex
SHA, or a git-worktree behind-count fallback when no `version.txt` is present) and
compare it against the latest published `v*` git tag for the given owner/repo.
Classifies the result as `current` / `behind <from> <to>` / `behind-clone <n> <ref>` /
`differs <to>` / `offline` / `source_is_live`. Advisory-only: every expected failure
mode (git absent, subprocess timeout, network unreachable) degrades to a status
string — the function itself never raises for those cases. sentinel.py's `probe_p19`
additionally treats an `ImportError` on this module, or any unexpected exception
escaping `release_currency_probe`, as "logic unavailable" (mapped to the `_absent`
ProbeNote) — see plan AC C3 (the Staff Engineer F6).

Port source: DoE-claude coordinator/lib/release-currency.sh (`release_currency_probe`)
at HEAD 0d9f70a4 — the `bash -c "source ..."` per-invocation subprocess this module
retires. sentinel.py's `probe_p19` now calls this in-process.
Spec backlink: pln-retire-claude-klabauter-s-doe-bash-bridg-5ab742 § Port C.

Negative-spec:
  - Highest-tag selection is a numeric-major.minor.patch **zero-padded** compare, NOT
    `packaging.version`/PEP440 semantics — a prerelease/build suffix (`-rc1`, `+build3`)
    is stripped and ignored for ordering purposes, exactly mirroring the bash
    `awk`+`sort` pipeline (plan AC C2, the Staff Engineer F4). PEP440 orders a prerelease *below*
    its corresponding release; the bash numeric-prefix-only compare does not guarantee
    that ordering. Do NOT "fix" this to be semver-prerelease-aware — it would silently
    change the emitted `<to>` label and the current/behind/differs classification.
  - The bash's 4th `source_is_live` auto-detect trigger — the probe *script itself*
    running from inside `install_root`'s own tree — has no native analog (this module
    never lives inside a coordinator-plugin install_root) and is deliberately dropped.
    See plan AC C2 (the Staff Engineer F1) for the full rationale; the remaining three triggers
    (env override, contributor-clone-registry `live_path` match, no-version.txt-and-
    not-a-worktree fallback) subsume it for claude-klabauter's install topology.
  - Do NOT reintroduce a `bash -c "source ..."` shim here — the whole point of this
    port is that the probe logic runs in-process. `git` subprocess calls with timeouts
    are fine (git, not bash).
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path
from typing import Optional, Tuple

from coordinator_core._settings_home import machine_local_dir

# Two named bounds, not eight literals — the shape hitlist G7 prescribes, kept
# module-local until `coordinator_core/git/run.py` lands and both can migrate
# onto it. The split is the one that matters: a LOCAL git read is process
# creation plus a plumbing query (`git --version` costs 25.3ms, DR-344 § 4), so
# 2.0s is a runaway guard four times over; a REMOTE leg is a network round trip
# that no rewrite of ours makes faster.
#
# DR-349 grants network no standing carve-out, and these are the sites that
# owe the design answer rather than a wider number: P-19 reaches two
# `ls-remote` legs and, on the no-version.txt path, a `fetch` — up to ~11s of
# remote I/O inside one op. That is why they are named here instead of being
# rounded up, and why the bound is 3/5 rather than the 30 this cluster's
# siblings carried. Do NOT raise either constant; a breach is a finding about
# where the probe runs, not about the number.
_LOCAL_GIT_TIMEOUT_SECS: float = 2.0
_REMOTE_GIT_TIMEOUT_SECS: float = 3.0
_REMOTE_FETCH_TIMEOUT_SECS: float = 5.0

_TAG_ALLOWLIST_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/+-]{0,127}$")
_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_TAG_REF_RE = re.compile(r"refs/tags/(v[^^]*)$")


def _load_tomllib():
    try:
        import tomllib  # type: ignore[import-not-found]

        return tomllib
    except ImportError:
        import tomli as tomllib  # type: ignore[no-redef]

        return tomllib


def _registry_live_path() -> str:
    """Read `plugin.mirrors.coordinator-claude.live_path` from the machine-local
    registry TOMLs under settings-home, per-key precedence: registry.local.toml
    wins, registry.toml fills the gap when the local file is absent or does not
    declare the key (matches `machine_resolver.registry_get` — the `.local`-only
    read predated that pattern; the bash oracle `_rc_registry_live_path` whose
    silent-degrade this mirrored is retired, so no parity constraint pins
    `.local`-only). Returns "" on any absence/parse failure — silent-degrade.
    Supports both the nested-table and flat dotted-key write forms."""
    ml = machine_local_dir()
    for fname in ("registry.local.toml", "registry.toml"):
        live = _live_path_from_file(ml / fname)
        if live:
            return live
    return ""


def _live_path_from_file(reg: Path) -> str:
    """Single-file `live_path` extraction (nested-table then flat dotted-key
    form), with the raw-Windows-path backslash-doubling parse retry. Returns ""
    on absence/parse failure so the caller can fall through to the next
    precedence rung."""
    if not reg.is_file():
        return ""
    try:
        tomllib = _load_tomllib()
    except ImportError:  # mirrors bash oracle's silent-degrade-to-empty (tomli absent)
        return ""
    raw = reg.read_text(encoding="utf-8")
    try:
        data = tomllib.loads(raw)
    except Exception:  # noqa: BLE001 — mirrors bash oracle's silent-degrade-to-empty
        # A hand-edited/generated registry.local.toml can carry a raw Windows
        # path (single backslashes) in a basic string, which TOML parses as
        # escape sequences (\U, \t, ...) and rejects outright. Retry once with
        # every backslash doubled (the valid TOML escape for a literal
        # backslash) before giving up — this only fires on the already-failed
        # parse, so a file that already used proper `\\` escaping (and thus
        # parsed cleanly on the first attempt) is never touched here.
        try:
            data = tomllib.loads(raw.replace("\\", "\\\\"))
        except Exception:  # noqa: BLE001 — still unparsable, silent-degrade
            return ""
    nested = data.get("plugin", {}).get("mirrors", {}).get("coordinator-claude", {})
    if isinstance(nested, dict):
        live = nested.get("live_path", "")
        if live:
            return str(live)
    val = data.get("plugin.mirrors.coordinator-claude.live_path", "")
    return str(val) if val else ""


def _run(cmd, timeout: float) -> Optional["subprocess.CompletedProcess[str]"]:
    """`subprocess.run` with a wall-clock timeout, folding `OSError` (incl. git
    absent -> FileNotFoundError) and `TimeoutExpired` into `None` — mirrors the
    bash `|| ...=""` / `|| return 1` error-tolerance chain built on `_rc_timeout_cmd`
    (cs_timeout)."""
    try:
        from coordinator_core.win_portability import no_console_creationflags

        return subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout,
            **no_console_creationflags(),
        )
    except (OSError, subprocess.TimeoutExpired):
        return None


def _first_field(out: str) -> str:
    """First whitespace-delimited field of the first non-blank line — mirrors
    `awk '{print $1}' | head -1` for every reachable input. (Divergence, unreachable
    in practice: a literal blank first line would make the bash pipeline yield ""
    while this helper skips ahead to the first non-blank line instead; `git
    ls-remote` output never starts blank at this module's call sites, so this is
    intentional and harmless, not a live parity gap.)"""
    for line in out.splitlines():
        parts = line.split()
        if parts:
            return parts[0]
    return ""


def _resolve_version_txt(install_root_str: str) -> Optional[str]:
    """Prints the 40-hex commit SHA in `<install-root>/version.txt`, or None if
    absent/malformed. Path is built by string concatenation (not `Path.__truediv__`)
    to reproduce the bash `"${install_root}/version.txt"` concatenation exactly when
    `install_root_str` is empty (bash yields `/version.txt`; `Path("") / "x"` would
    instead yield the *relative* `Path("x")`, silently diverging)."""
    vtxt = Path(f"{install_root_str}/version.txt")
    if not vtxt.is_file():
        return None
    try:
        raw = vtxt.read_text(encoding="utf-8")
    except OSError:
        return None
    sha = "".join(raw.split())
    return sha if _SHA_RE.match(sha) else None


def _awk_num(s: str) -> int:
    """Mirrors awk's string-to-number coercion (`a[i]+0`): parse a leading
    optionally-signed integer prefix, else 0."""
    m = re.match(r"^-?\d+", s)
    return int(m.group(0)) if m else 0


def _select_highest_tag(tags) -> Optional[str]:
    """Highest `v*` tag by zero-padded numeric major.minor.patch compare, lexical
    tiebreak — mirrors the bash `awk '{split(...,a,/[.+-]/); printf "%010d..."}
    | sort | tail -1'` pipeline. Prerelease/build suffixes are stripped by the split
    and never consulted for ordering (the Staff Engineer F4 — do not use PEP440 semantics)."""
    # Review: code-reviewer -- dedup only; `sorted()` is dead preprocessing, the
    # 4-tuple `key` below (numeric parts + tag string) already fully determines
    # both the numeric ordering and the lexical tie-break for `max`.
    uniq = set(tags)
    if not uniq:
        return None

    def key(tag: str):
        t = tag[1:] if tag.startswith("v") else tag
        parts = re.split(r"[.+-]", t)

        def num(i: int) -> int:
            return _awk_num(parts[i]) if i < len(parts) else 0

        return (num(0), num(1), num(2), tag)

    return max(uniq, key=key)


def _fetch_latest_release_tag(owner_repo: str, force_offline: bool) -> Optional[str]:
    if force_offline:
        return None
    repo_url = f"https://github.com/{owner_repo}.git"
    proc = _run(["git", "ls-remote", "--tags", repo_url, "refs/tags/v*"], timeout=_REMOTE_GIT_TIMEOUT_SECS)
    if proc is None or proc.returncode != 0 or not (proc.stdout or "").strip():
        return None
    tags = []
    for line in proc.stdout.splitlines():
        m = _TAG_REF_RE.search(line)
        if m:
            tags.append(m.group(1))
    return _select_highest_tag(tags)


def _resolve_tag_sha(owner_repo: str, tag: str, force_offline: bool) -> Optional[str]:
    """Resolves `tag` to its COMMIT sha via `git ls-remote`, dereferencing an
    annotated tag's `^{}` peel first (an annotated tag's own object SHA is NOT the
    commit SHA); falls back to the bare ref for lightweight tags. Never uses a
    Release-API `target_commitish` (a ref name, often a branch, not a resolvable
    SHA)."""
    if force_offline:
        return None
    repo_url = f"https://github.com/{owner_repo}.git"
    proc = _run(["git", "ls-remote", repo_url, f"refs/tags/{tag}^{{}}"], timeout=_REMOTE_GIT_TIMEOUT_SECS)
    out = (proc.stdout or "") if proc is not None and proc.returncode == 0 else ""
    if not out.strip():
        proc = _run(["git", "ls-remote", repo_url, f"refs/tags/{tag}"], timeout=_REMOTE_GIT_TIMEOUT_SECS)
        out = (proc.stdout or "") if proc is not None and proc.returncode == 0 else ""
    sha = _first_field(out)
    return sha if _SHA_RE.match(sha) else None


def _git_clone_behind_count(
    install_root_str: str, force_offline: bool
) -> Optional[Tuple[int, str]]:
    """Called when `install_root` has no `version.txt` but IS a git work-tree.
    Fetches the tracked remote then counts commits in `HEAD..<upstream>` (preferring
    the configured `@{u}`, falling back to `origin/main`). Returns None on any
    failure (git absent, no remote, fetch failure, force-offline) — caller treats
    that as `offline`."""
    if force_offline:
        return None
    fetch = _run(["git", "-C", install_root_str, "fetch", "-q"], timeout=_REMOTE_FETCH_TIMEOUT_SECS)
    if fetch is None or fetch.returncode != 0:
        return None

    count_str = ""
    ref = ""
    count_proc = _run(
        ["git", "-C", install_root_str, "rev-list", "--count", "HEAD..@{u}"], timeout=_LOCAL_GIT_TIMEOUT_SECS
    )
    if count_proc is not None and count_proc.returncode == 0 and count_proc.stdout.strip():
        count_str = count_proc.stdout.strip()
        ref_proc = _run(
            ["git", "-C", install_root_str, "rev-parse", "--abbrev-ref", "@{u}"], timeout=_LOCAL_GIT_TIMEOUT_SECS
        )
        ref = (
            ref_proc.stdout.strip()
            if ref_proc is not None and ref_proc.returncode == 0 and ref_proc.stdout.strip()
            else "@{u}"
        )
    else:
        origin_check = _run(
            ["git", "-C", install_root_str, "rev-parse", "origin/main"], timeout=_LOCAL_GIT_TIMEOUT_SECS
        )
        if origin_check is None or origin_check.returncode != 0:
            return None
        count_proc = _run(
            ["git", "-C", install_root_str, "rev-list", "--count", "HEAD..origin/main"],
            timeout=_LOCAL_GIT_TIMEOUT_SECS,
        )
        if count_proc is None or count_proc.returncode != 0 or not count_proc.stdout.strip():
            return None
        count_str = count_proc.stdout.strip()
        ref = "origin/main"

    if not count_str.isdigit():
        return None
    return int(count_str), ref


def _check_ancestry(install_root_str: str, local_sha: str, tag_sha: str) -> bool:
    """`git merge-base --is-ancestor` — used only to enrich the "behind" case.
    Requires a git checkout at install_root."""
    if not Path(f"{install_root_str}/.git").exists():
        return False
    proc = _run(
        ["git", "-C", install_root_str, "merge-base", "--is-ancestor", local_sha, tag_sha],
        timeout=_LOCAL_GIT_TIMEOUT_SECS,
    )
    return proc is not None and proc.returncode == 0


def _local_describe_tag(install_root_str: str, local_sha: str) -> str:
    """Human-readable local tag ref, or the first 12 chars of the SHA."""
    if Path(f"{install_root_str}/.git").exists():
        proc = _run(
            ["git", "-C", install_root_str, "describe", "--tags", "--exact-match", local_sha],
            timeout=_LOCAL_GIT_TIMEOUT_SECS,
        )
        if proc is not None and proc.returncode == 0 and proc.stdout.strip():
            return proc.stdout.strip()
        proc = _run(
            ["git", "-C", install_root_str, "describe", "--tags", local_sha], timeout=_LOCAL_GIT_TIMEOUT_SECS
        )
        if proc is not None and proc.returncode == 0 and proc.stdout.strip():
            return proc.stdout.strip()
    return local_sha[:12]


def release_currency_probe(
    plugin: str, owner_repo: str, install_root: Optional[Path]
) -> str:
    """Native port of `release-currency.sh::release_currency_probe`.

    Args:
        plugin: informational plugin name (unused in the classification logic
            itself — the bash original declares-but-never-consumes it too).
        owner_repo: "owner/repo" for the GitHub tag source.
        install_root: the coordinator plugin install root (may be None — treated
            as the empty string, matching the bash caller's `${coordinator_root
            or ""}` convention).

    Returns one of: `source_is_live` / `current` / `"behind <from> <to>"` /
    `"behind-clone <n> <ref>"` / `"differs <to>"` / `offline`. Advisory contract:
    every expected failure mode (subprocess error, git absent, network
    unreachable, timeout) degrades to a status string rather than raising.
    """
    del plugin  # informational only — see docstring
    root_str = str(install_root) if install_root else ""
    force_offline = os.environ.get("RELEASE_CURRENCY_FORCE_OFFLINE", "0") == "1"

    # 1. source_is_live via explicit env override (the auto-detect-in-tree
    #    trigger is deliberately dropped — see module docstring negative-spec).
    if os.environ.get("COORDINATOR_CURRENCY_SOURCE_IS_LIVE", "0") == "1":
        return "source_is_live"

    # 2. Resolve local installed version.
    local_sha = _resolve_version_txt(root_str)
    if local_sha is None:
        # No version.txt — contributor-clone registry guard first (an authoring/
        # contributor box has a git clone but no version.txt and IS source_is_live).
        live_path = _registry_live_path()
        if live_path and live_path.rstrip("/") == root_str.rstrip("/"):
            return "source_is_live"

        is_worktree = _run(
            ["git", "-C", root_str, "rev-parse", "--is-inside-work-tree"], timeout=_LOCAL_GIT_TIMEOUT_SECS
        )
        if is_worktree is not None and is_worktree.returncode == 0:
            behind = _git_clone_behind_count(root_str, force_offline)
            if behind is None:
                return "offline"
            count, ref = behind
            return f"behind-clone {count} {ref}" if count > 0 else "current"

        # Not a git work-tree and no version.txt — genuinely no managed install.
        return "source_is_live"

    # 3. Fetch latest published release tag.
    latest_tag = _fetch_latest_release_tag(owner_repo, force_offline)
    if latest_tag is None:
        return "offline"

    # Security: the tag is a network-controlled string that flows into a
    # `git ls-remote` refspec — validate at this single chokepoint before any
    # interpreter sees it (blocks a leading '-' being read as a git option).
    if not _TAG_ALLOWLIST_RE.match(latest_tag):
        return "offline"

    # 4. Resolve tag -> commit SHA via git ls-remote (never target_commitish).
    tag_sha = _resolve_tag_sha(owner_repo, latest_tag, force_offline)
    if tag_sha is None:
        return "offline"

    # 5. Classify.
    if local_sha == tag_sha:
        return "current"

    if _check_ancestry(root_str, local_sha, tag_sha):
        from_label = _local_describe_tag(root_str, local_sha)
        return f"behind {from_label} {latest_tag}"
    return f"differs {latest_tag}"
