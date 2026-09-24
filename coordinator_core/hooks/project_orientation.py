"""
coordinator_core.hooks.project_orientation — warm-door port of DoE-claude's
`coordinator/hooks/scripts/project-orientation.py`, **`--lightweight` LEG
ONLY**.

SCOPE FENCE (per this chunk's own row body — measure-first rule), coverage
established against the source script's own `main()` at DoE-claude HEAD:
the source's ONLY production invocation is `sessionstart-dispatch.py`'s
`REGISTRY` entry, `argv=["--lightweight"]` — "no other caller exists,
repo-wide grep" (source module docstring). `main()`'s non-lightweight
("full/legacy") branch — `resolve_repo_root()`'s `git rev-parse` spawn,
`handle_cache_present()`'s three git-diff spawns, and the whole `full_mode()`
function (scc, git log, pointer-doc scan) — is dead weight on the reachable
leg and is NOT PORTED. Porting it would be exactly the "porting lines that
are already unreachable" waste `docs/reference/warm-hook-migration.md`
names. `resolve_repo_root`, `handle_cache_present`, `full_mode`,
`_count_lines`, `_pointer_doc`, `_resolve_scc_cmd` have no analogue here.

`params` reaches this op in either shape a `hooks.*` handler receives —
wrapped as `params["payload"]` by both engine doors, flat by the cold chain;
`_envelope.payload_of` reads both. Every input comes from that payload —
never from `os.environ` or this process's own `cwd`/session. The resident
engine serves ~50 concurrent
sessions; env/cwd reads that the source script (a per-invocation CLI) took
from its own process now read `payload["env"]`/`payload["cwd"]` instead,
matching the sibling `hooks.*` ops in this family
(`plan_persistence_check`, `runtime_tripwire_em_check`). `_read_stdin()` (the
source's B-F2 stdin-drain defensiveness) has no analogue — the warm door
never has a stdin pipe to drain; the payload already carries everything the
harness would otherwise have written there.

Zero-spawn throughout, matching the source's own 2026-07-15 PM directive
(every subprocess spawn costs ~200-500ms on Windows, and this hook's output
gates first-token-to-context on every SessionStart). Where the source
already had a zero-spawn boot variant (`resolve_repo_root_boot`,
`handle_cache_present_boot`, `_read_current_branch_boot`,
`_read_current_full_sha_boot`), this module either ports that variant or —
where claude-klabauter already carries the identical zero-spawn primitive — calls
the existing one instead of re-deriving it:
    - `resolve_repo_root_boot()` -> `coordinator_core.git.repo_root.
      show_toplevel` (memoized, non-spawning parent walk) fed
      `payload["env"]["CLAUDE_PROJECT_DIR"]` first, `payload["cwd"]` second —
      same two-rung order the source used, minus its manual `.git`-marker
      walk (show_toplevel already does that walk, and does it memoized).
    - `_read_current_branch_boot()` / `_read_current_full_sha_boot()` ->
      `coordinator_core.git.git_state.head_branch` /
      `coordinator_core.git.git_state.head_sha` (already-ported, zero-spawn
      `.git/HEAD` + packed-refs readers; PORTED (residue) only for the
      thin adaptation these two callers need — see `_branch_boot`/
      `_sha_boot` below for the return-shape reconciliation).
    - `_claude_home()` / `_settings_home()` -> `coordinator_core.
      _settings_home.home_dir()` / `settings_home()` (the canonical
      resolver this row's body names — "Import the support layer, never a
      DoE path").
    - `_resolve_claude_klabauter_root_native()` (the fuller, still-zero-spawn-on-rungs
      1/1.5 ladder; rung 2 shells `machine-local` and is never reached on
      this boot leg — see below) -> `coordinator_core.engine_root.
      coordinator_engine_root_with_class()`, wrapped fail-open (raises on a
      hard miss — `RuntimeError` from the shim-load rung, or whatever the
      shim's own gate raises; every caller here degrades to "no engine
      resolvable" on any exception, never raising into the boot path).

`engine_resolution_banner()` — REDUCED SCOPE, not a straight port. The
source's version answers "which sibling claude-klabauter checkout will THIS session's
hooks execute", including a publish-mirror roster (`resolve_publish_mirror_
roster`), a provenance suffix (env-override / published-fallback /
published-legacy-gate / live-no-target / live-env-dup), and a
`CLAUDE_PLUGIN_ROOT`-divergence line (`_w_live_plugin_root_line`) — all of
it DoE-side machinery for a script running OUTSIDE the engine, reasoning
about which sibling checkout answers it. Hosted here, INSIDE the engine's
own warm process, the question "which checkout answered this hook fire" has
one honest answer — this process's own resolved root and class, via the
same `coordinator_engine_root_with_class()` this module already imports —
and the roster/provenance/plugin-root legs (DoE-plane concepts: publish
targets, `CLAUDE_PLUGIN_ROOT`, dev-vs-install layout) have no live consumer
once the code that asks the question and the code that answers it are the
same process. Ported: the class line (published engine / live working
tree) plus the branch suffix (`coordinator_core.git.git_state.head_branch`,
zero-spawn). NOT ported: `resolve_publish_mirror_roster`,
`resolve_own_publish_target_keys`, the provenance suffix, and
`_w_live_plugin_root_line`/`_paths_differ` (no other caller — dropped with
this banner's roster leg).

`foreign_path_filter.session_repo_is_plane` (this row's other write) is
consumed by nothing on the ported surface once the mirror-roster leg above
is out of scope — its sibling module docstring notes it stands alone as a
generic plane-repo predicate; kept as its own file per this row's declared
`writes`.

Response shape: `context_only("SessionStart", text)` — the additionalContext
envelope shape `hooks/_envelope.py` already exports, matching every other
context-emitting hook op in this package. Never raises: every banner
section is independently try/except-wrapped exactly as the source script's
`main()` wraps each call, so one banner's failure never suppresses its
siblings or the cache/lightweight tail.

Measurement (this row's own "measure-first" instruction): see
`state/audits/doe-script-arrivals/W4-C11.yaml` — cold-import + a representative
fire, both well under the 500ms bar; every banner section on this leg is a
handful of `stat()`/small-file reads, no spawn.
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, List, Mapping, Optional

from coordinator_core._settings_home import home_dir, settings_home
from coordinator_core.engine_root import (
    _RESOLUTION_LIVE_WORKING_TREE_LITERAL as RESOLUTION_LIVE_WORKING_TREE,
    _RESOLUTION_RESOLVED_ENGINE_LITERAL as RESOLUTION_RESOLVED_ENGINE,
    coordinator_engine_root_with_class,
)
from coordinator_core.git.git_state import head_branch, head_sha
from coordinator_core.git.repo_root import show_toplevel
from coordinator_core.hooks._envelope import context_only, payload_of
from coordinator_core.ipc import register_op

_GENERATED_AT_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
_CURRENCY_BANNER_STALE_HOURS = 24
_ORIENTATION_STALENESS_GRACE_MINUTES_DEFAULT = 30
_ORIENTATION_STALENESS_DRIFT_MAX_ENTRIES_DEFAULT = 5
_REFLOG_TAIL_WINDOW_BYTES = 65536


# ---------------------------------------------------------------------------
# payload-scoped helpers (replace the source script's os.environ/cwd reads)
# ---------------------------------------------------------------------------


def _env_get(env: Mapping[str, Any], name: str) -> str:
    val = env.get(name) if isinstance(env, Mapping) else None
    return val if isinstance(val, str) else ""


def _mtime_epoch(path: Path) -> Optional[int]:
    try:
        return int(path.stat().st_mtime)
    except Exception:
        return None


def _resolve_repo_root(env: Mapping[str, Any], cwd: str) -> Optional[str]:
    """Zero-spawn repo-root resolution: `CLAUDE_PROJECT_DIR` first (trusted
    as-is, matching the source's `resolve_repo_root_boot` rung 1), else a
    memoized, non-spawning parent walk from `cwd` via `show_toplevel`."""
    env_root = _env_get(env, "CLAUDE_PROJECT_DIR")
    if env_root and Path(env_root).is_dir():
        return env_root
    if cwd:
        return show_toplevel(cwd)
    return None


def _resolve_claude_klabauter_root_fast() -> Optional[str]:
    """Rung 1.5: a pointer-FILE read only — no ladder, no subprocess."""
    try:
        ptr = settings_home() / "machine-local" / ".claude-klabauter-live-root"
        val = ptr.read_text(encoding="utf-8").strip()
    except (OSError, RuntimeError):
        return None
    if val and Path(val).is_dir():
        return val
    return None


def _resolve_state_root(repo_root: Optional[str]) -> str:
    """Port of `resolve_state_root(repo_root, boot=True)`: the common case
    (any sibling repo) is a zero-subprocess `<repo_root>/state` join; only
    when `repo_root` IS the meta-repo (`~/.claude`) does this redirect to
    the claude-klabauter pointer-file's own `state/` dir. Boot fast-path: no
    bash-source fallback (the source's rare native-resolver fail-safe is
    skipped on `boot=True` there too — see its own docstring)."""
    if not repo_root:
        return str(Path(".") / "state")

    try:
        meta_dir = home_dir() / ".claude"
        is_meta = Path(repo_root).resolve() == meta_dir.resolve()
    except Exception:
        is_meta = False

    if not is_meta:
        return str(Path(repo_root) / "state")

    claude_klabauter_root = _resolve_claude_klabauter_root_fast()
    if claude_klabauter_root:
        return str(Path(claude_klabauter_root) / "state")
    return str(Path(repo_root) / "state")


def _branch_boot(repo_root: Optional[str]) -> str:
    """`head_branch()`'s return shape reconciled to the source script's
    `_read_current_branch_boot()` contract: `""` (not `"HEAD"`) on a
    detached HEAD — the source treats detached as "no branch name",
    matching the old `git rev-parse --abbrev-ref HEAD` failure shape."""
    if not repo_root:
        return ""
    try:
        branch = head_branch(repo_root)
    except Exception:
        return ""
    if not branch or branch == "HEAD":
        return ""
    return branch


def _sha_boot(repo_root: Optional[str]) -> str:
    if not repo_root:
        return ""
    try:
        return head_sha(repo_root) or ""
    except Exception:
        return ""


# ---------------------------------------------------------------------------
# Staleness banners (repo map / exec summary / peer re-check / harness drift)
# ---------------------------------------------------------------------------


def _staleness_banner(
    out: List[str],
    env: Mapping[str, Any],
    *,
    env_off_var: str,
    get_age_hours,
    stale_threshold: int,
    very_stale_threshold: int,
    stale_message,
    very_stale_message,
) -> None:
    if _env_get(env, env_off_var):
        return
    age_hours = get_age_hours()
    if age_hours is None:
        return
    if age_hours >= very_stale_threshold:
        out.append("\n")
        out.append(very_stale_message(age_hours))
    elif age_hours >= stale_threshold:
        out.append(stale_message(age_hours))


def _repomap_staleness_banner(out: List[str], env: Mapping[str, Any], repo_root: Optional[str]) -> None:
    def _get_age_hours() -> Optional[int]:
        rm_repomap = Path(repo_root or ".") / ".claude" / "repomap.md"
        if not rm_repomap.is_file():
            return None
        epoch = _mtime_epoch(rm_repomap)
        if epoch is None:
            return None
        return (int(time.time()) - epoch) // 3600

    _staleness_banner(
        out,
        env,
        env_off_var="COORDINATOR_REPOMAP_STATUS_OFF",
        get_age_hours=_get_age_hours,
        stale_threshold=24,
        very_stale_threshold=168,
        very_stale_message=lambda h: (
            f"── ⚠ Repo map VERY STALE: {h}h old — regenerate via /update-docs ──\n"
        ),
        stale_message=lambda h: (
            f"── Repo map stale: {h}h old — refresh via /update-docs ──\n"
        ),
    )


def _exec_summary_staleness_banner(out: List[str], env: Mapping[str, Any], repo_root: Optional[str]) -> None:
    def _get_age_hours() -> Optional[int]:
        es_doc = Path(repo_root or ".") / "docs" / "exec-summary.md"
        if not es_doc.is_file():
            return None
        epoch = _mtime_epoch(es_doc)
        if epoch is None:
            return None
        return (int(time.time()) - epoch) // 3600

    _staleness_banner(
        out,
        env,
        env_off_var="COORDINATOR_EXECSUMMARY_STATUS_OFF",
        get_age_hours=_get_age_hours,
        stale_threshold=24,
        very_stale_threshold=168,
        very_stale_message=lambda h: (
            f"── ⚠ Exec-summary VERY STALE: {h}h old — refresh via /workweek-start ──\n"
        ),
        stale_message=lambda h: (
            f"── Exec-summary stale: {h}h old — refresh via /workweek-start ──\n"
        ),
    )


def _parse_peer_last_checked_epoch(raw: str) -> int:
    if not raw or raw.lower() == "null":
        return 0
    try:
        dt = datetime.strptime(raw, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        return int(dt.timestamp())
    except Exception:
        return 0


def _peer_recheck_staleness_banner(out: List[str], env: Mapping[str, Any], repo_root: Optional[str]) -> None:
    def _get_age_hours() -> Optional[int]:
        peers_dir = Path(repo_root or ".") / "state" / "peers"
        if not peers_dir.is_dir():
            return None
        try:
            entries = [p for p in peers_dir.iterdir() if p.is_file() and p.suffix in (".yaml", ".yml")]
        except Exception:
            return None
        if not entries:
            return None
        oldest_epoch: Optional[int] = None
        for entry in entries:
            try:
                text = entry.read_text(encoding="utf-8", errors="replace")
            except Exception:
                continue  # per-peer-file probe; one unreadable entry must not abort the scan
            raw = _extract_cache_field(text, "last_checked")
            epoch = _parse_peer_last_checked_epoch(raw)
            if oldest_epoch is None or epoch < oldest_epoch:
                oldest_epoch = epoch
        if oldest_epoch is None:
            return None
        return (int(time.time()) - oldest_epoch) // 3600

    _staleness_banner(
        out,
        env,
        env_off_var="COORDINATOR_PEER_RECHECK_STATUS_OFF",
        get_age_hours=_get_age_hours,
        stale_threshold=168,
        very_stale_threshold=720,
        very_stale_message=lambda h: (
            f"── ⚠ Peer set VERY STALE: oldest re-check {h}h old — run a code-comparison "
            "re-check against your peers (see state/peers/*.yaml, "
            "coordinator/pipelines/deep-research/repo-driver.md) ──\n"
        ),
        stale_message=lambda h: (
            f"── Peer set stale: oldest re-check {h}h old — consider a code-comparison "
            "re-check against your peers (state/peers/*.yaml) ──\n"
        ),
    )


def _harness_version_drift_banner(out: List[str], env: Mapping[str, Any], repo_root: Optional[str]) -> None:
    if _env_get(env, "COORDINATOR_HARNESS_DRIFT_STATUS_OFF"):
        return

    pin_path = Path(repo_root or ".") / "state" / "reference" / "anthropic-docs" / "reconciled-against.json"
    if not pin_path.is_file():
        return
    try:
        pin_data = json.loads(pin_path.read_text(encoding="utf-8"))
    except Exception:
        return

    pinned = pin_data.get("reconciled_against_harness_version") if isinstance(pin_data, dict) else None
    if not isinstance(pinned, str) or not pinned:
        return

    try:
        versions_dir = home_dir() / ".local" / "share" / "claude" / "versions"
        if not versions_dir.is_dir():
            return
        entries = os.listdir(versions_dir)
    except Exception:
        return

    def _parse(v: str) -> Optional[tuple]:
        parts = v.split(".")
        if not parts:
            return None
        out_parts = []
        for p in parts:
            if not p.isdigit():
                return None
            out_parts.append(int(p))
        return tuple(out_parts)

    pinned_tuple = _parse(pinned)
    if pinned_tuple is None:
        return

    newest_tuple: Optional[tuple] = None
    newest_str: Optional[str] = None
    for entry in entries:
        candidate = _parse(entry)
        if candidate is None:
            continue
        if newest_tuple is None or candidate > newest_tuple:
            newest_tuple = candidate
            newest_str = entry

    if newest_tuple is None or newest_str is None or newest_tuple <= pinned_tuple:
        return

    leading_match = newest_tuple[:-1] == pinned_tuple[:-1] and len(newest_tuple) == len(pinned_tuple)
    n_desc = f"{newest_tuple[-1] - pinned_tuple[-1]} release(s)" if leading_match else "major/minor move"
    small_patch_move = leading_match and (newest_tuple[-1] - pinned_tuple[-1]) < 5

    if small_patch_move:
        out.append(
            f"── Harness moved: {pinned} → {newest_str} ({n_desc}) — vendored Claude Code "
            f"docs reconciled at {pinned}; re-read what shipped in the gap ──\n"
        )
    else:
        out.append("\n")
        out.append(
            f"── ⚠ Harness drift: {pinned} → {newest_str} — vendored Claude Code docs are "
            f"{n_desc} behind; re-read the delta and re-pin "
            "state/reference/anthropic-docs/reconciled-against.json ──\n"
        )


# ---------------------------------------------------------------------------
# Install/corpus/tier currency banners
# ---------------------------------------------------------------------------


def _local_surface_probe_value(parsed: dict, json_path: str):
    if not isinstance(parsed, dict):
        return None
    return parsed.get(json_path)


def _local_install_surface_banner(out: List[str], env: Mapping[str, Any], repo_root: Optional[str]) -> None:
    if _env_get(env, "COORDINATOR_INSTALL_SURFACE_STATUS_OFF"):
        return

    manifest_path = Path(repo_root or ".") / "coordinator" / "docs" / "install" / "agent-install-manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except Exception:
        return
    if not isinstance(manifest, dict):
        return

    surfaces = manifest.get("required_local_surfaces")
    if not isinstance(surfaces, list):
        return

    file_cache: dict = {}
    for entry in surfaces:
        if not isinstance(entry, dict):
            continue
        surface_id = entry.get("id")
        probe = entry.get("probe")
        install = entry.get("install") if isinstance(entry.get("install"), dict) else {}
        remediation = install.get("remediation")
        if not isinstance(surface_id, str) or not isinstance(probe, dict):
            continue
        if not isinstance(remediation, str) or not remediation:
            continue

        kind = probe.get("kind")
        if kind not in ("json_key_present", "json_object_key_true"):
            continue

        location = probe.get("location")
        relative_path = probe.get("relative_path")
        json_path = probe.get("json_path")
        if location != "install_root" or not isinstance(relative_path, str) or not isinstance(json_path, str):
            continue

        try:
            target_path = home_dir() / relative_path
            cache_key = str(target_path)
        except Exception:
            continue  # per-surface probe entry; a malformed path must not abort the scan

        if cache_key not in file_cache:
            try:
                file_cache[cache_key] = json.loads(target_path.read_text(encoding="utf-8"))
            except Exception:
                file_cache[cache_key] = None

        target_parsed = file_cache[cache_key]
        if not isinstance(target_parsed, dict):
            continue

        if kind == "json_key_present":
            present = _local_surface_probe_value(target_parsed, json_path) is not None
        else:
            key = probe.get("key")
            if not isinstance(key, str):
                continue
            obj = _local_surface_probe_value(target_parsed, json_path)
            present = isinstance(obj, dict) and bool(obj.get(key))

        if present:
            continue

        out.append(f"── ⚠ Install surface missing on THIS machine: {surface_id} — {remediation} ──\n")


def _install_currency_banner(out: List[str], env: Mapping[str, Any]) -> None:
    if _env_get(env, "COORDINATOR_CURRENCY_STATUS_OFF"):
        return
    try:
        cache_path = home_dir() / ".claude" / "plugins" / "coordinator-claude" / "data" / "doctor-last-run.json"
    except Exception:
        return
    try:
        cache_text = cache_path.read_text(encoding="utf-8")
    except Exception:
        out.append(
            "── Install currency: absent — no doctor-last-run.json cache found; "
            "/workday-start populates it ──\n"
        )
        return

    parsed = None
    try:
        parsed = json.loads(cache_text)
    except Exception:
        parsed = None

    ran_at_raw = parsed.get("ran_at") if isinstance(parsed, dict) else None
    age_hours: Optional[float] = None
    if isinstance(ran_at_raw, str) and ran_at_raw:
        try:
            ran_at_dt = datetime.strptime(ran_at_raw, _GENERATED_AT_FORMAT).replace(tzinfo=timezone.utc)
            age_hours = (datetime.now(timezone.utc) - ran_at_dt).total_seconds() / 3600.0
        except Exception:
            age_hours = None

    if not isinstance(parsed, dict) or age_hours is None:
        out.append(
            "── Install currency: stale-unknown (verdict cache unparseable) — "
            "/workday-start refreshes it ──\n"
        )
        return

    if age_hours >= _CURRENCY_BANNER_STALE_HOURS:
        out.append(
            f"── Install currency: stale-unknown ({age_hours:.0f}h old, refresh window is "
            f"{_CURRENCY_BANNER_STALE_HOURS}h) — /workday-start refreshes it ──\n"
        )
        return

    advisory_notes = parsed.get("advisory_notes")
    if advisory_notes is not None and not isinstance(advisory_notes, list):
        out.append(
            "── Install currency: stale-unknown (verdict cache malformed — advisory_notes is "
            "not a list) — /workday-start refreshes it ──\n"
        )
        return

    p19_line: Optional[str] = None
    if isinstance(advisory_notes, list):
        for note in advisory_notes:
            if isinstance(note, str) and note.startswith("P-19: "):
                p19_line = note
                break
    if p19_line:
        out.append(f"── {p19_line} ──\n")


def _corpus_currency_banner(out: List[str], env: Mapping[str, Any]) -> None:
    if _env_get(env, "COORDINATOR_CURRENCY_STATUS_OFF"):
        return
    try:
        sentinel_path = (
            home_dir() / ".claude" / "plugins" / "coordinator-claude" / "data" / "corpus-currency-last-run.json"
        )
    except Exception:
        return
    try:
        sentinel_text = sentinel_path.read_text(encoding="utf-8")
    except Exception:
        return

    parsed = None
    try:
        parsed = json.loads(sentinel_text)
    except Exception:
        parsed = None

    ran_at_raw = parsed.get("ran_at") if isinstance(parsed, dict) else None
    age_hours: Optional[float] = None
    if isinstance(ran_at_raw, str) and ran_at_raw:
        try:
            ran_at_dt = datetime.strptime(ran_at_raw, _GENERATED_AT_FORMAT).replace(tzinfo=timezone.utc)
            age_hours = (datetime.now(timezone.utc) - ran_at_dt).total_seconds() / 3600.0
        except Exception:
            age_hours = None

    if not isinstance(parsed, dict) or age_hours is None:
        out.append(
            "── Corpus currency: stale-unknown (verdict cache unparseable) — "
            "/workday-start refreshes it ──\n"
        )
        return

    if age_hours >= _CURRENCY_BANNER_STALE_HOURS:
        out.append(
            f"── Corpus currency: stale-unknown ({age_hours:.0f}h old, refresh window is "
            f"{_CURRENCY_BANNER_STALE_HOURS}h) — /workday-start refreshes it ──\n"
        )
        return

    bands_present = "bands" in parsed
    bands = parsed.get("bands")
    if bands_present and not isinstance(bands, list):
        out.append(
            "── Corpus currency: stale-unknown (verdict cache malformed — bands is not a "
            "list) — /workday-start refreshes it ──\n"
        )
        return
    if not isinstance(bands, list):
        return

    for band in bands:
        if not isinstance(band, dict) or band.get("verdict") != "behind":
            continue
        name = band.get("band")
        remount_command = band.get("remount_command")
        out.append(f"── Corpus currency: {name} is behind — refresh: {remount_command} ──\n")


def _load_tier_last_run_module():
    """Import `coordinator/bin/tier-last-run.py` by file path, DoE-relative.

    Unlike the source script (which resolves this relative to its own
    `__file__`, a sibling of the file it imports), this hook runs from
    claude-klabauter's own checkout — `tier-last-run.py` is a DoE-plane script this
    repo does not carry a copy of. Resolves via the same engine-root
    primitive this module already imports rather than inventing a second
    ladder; fails open to `None` (every caller here degrades on `None`,
    matching the source's own contract) when no DoE checkout is resolvable.
    """
    try:
        import importlib.util

        root, _klass = _resolve_engine_root_with_class()
        if not root:
            return None
        module_path = Path(root).resolve().parent / "coordinator" / "bin" / "tier-last-run.py"
        spec = importlib.util.spec_from_file_location("_tier_last_run_c2", module_path)
        if spec is None or spec.loader is None:
            return None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    except Exception:
        return None


def _tier_currency_banner(out: List[str], env: Mapping[str, Any], repo_root: Optional[str]) -> None:
    if _env_get(env, "COORDINATOR_CURRENCY_STATUS_OFF"):
        return

    root = Path(repo_root).resolve() if repo_root else Path.cwd()
    module = _load_tier_last_run_module()
    if module is None:
        return

    try:
        entries = module._ceremony_entries(root)
    except Exception:
        return
    if not entries:
        return

    state_path = root / "state" / "tier-last-run.json"
    state = module._load_state(state_path)

    for entry in entries:
        if not isinstance(entry, dict):
            continue
        name = entry.get("name")
        if not name:
            continue
        record = state.get(name)
        if not isinstance(record, dict) or "ran_at" not in record:
            out.append(
                f"── {name}: unknown — no run recorded; "
                f"`tier-last-run record --entry {name} ...` records one ──\n"
            )
            continue

        ran_at_raw = record.get("ran_at")
        age_hours: Optional[float] = None
        if isinstance(ran_at_raw, str) and ran_at_raw:
            try:
                ran_at_dt = datetime.strptime(ran_at_raw, module._ISO_FORMAT)
                if ran_at_dt.tzinfo is None:
                    ran_at_dt = ran_at_dt.replace(tzinfo=timezone.utc)
                age_hours = (datetime.now(timezone.utc) - ran_at_dt).total_seconds() / 3600.0
            except Exception:
                age_hours = None

        if age_hours is None:
            out.append(f"── {name}: stale (ran_at unparseable) — record a fresh run to clear ──\n")
            continue

        human_age = module._format_age(ran_at_raw)
        if age_hours < 0 or age_hours >= _CURRENCY_BANNER_STALE_HOURS:
            out.append(
                f"── {name}: stale ({human_age}, refresh window is "
                f"{_CURRENCY_BANNER_STALE_HOURS}h) ──\n"
            )
            continue
        out.append(f"── {name}: last ran {human_age} ──\n")


# ---------------------------------------------------------------------------
# Engine-resolution banner (REDUCED SCOPE — see module docstring)
# ---------------------------------------------------------------------------


def _resolve_engine_root_with_class() -> tuple:
    try:
        return coordinator_engine_root_with_class()
    except Exception:
        return None, None


def _engine_resolution_banner(out: List[str]) -> None:
    root, klass = _resolve_engine_root_with_class()
    if not root or not klass:
        return

    branch_suffix = ""
    try:
        branch = _branch_boot(root)
        if branch:
            branch_suffix = f" (branch {branch})"
        else:
            sha = _sha_boot(root)
            if sha:
                branch_suffix = f" (detached at {sha[:8]})"
    except Exception:
        branch_suffix = ""

    if klass == RESOLUTION_RESOLVED_ENGINE:
        out.append(f"── Engine: published engine mirror{branch_suffix} — this session's hooks resolve to {root} ──\n")
    elif klass == RESOLUTION_LIVE_WORKING_TREE:
        out.append(f"── Engine: sibling LIVE working tree{branch_suffix} — this session's hooks resolve here ──\n")


# ---------------------------------------------------------------------------
# Orientation-cache staleness + cache-present / lightweight branches
# ---------------------------------------------------------------------------


def _extract_cache_field(cache_text: str, key: str) -> str:
    prefix = f"{key}:"
    for line in cache_text.splitlines():
        if line.startswith(prefix):
            val = line[len(prefix):].strip().strip("\"'")
            val = val.replace(" ", "")
            return val
    return ""


def _extract_cache_head(cache_text: str) -> str:
    return _extract_cache_field(cache_text, "git_head_at_generation")


def _cache_age_minutes(cache_text: str) -> Optional[float]:
    raw = _extract_cache_field(cache_text, "generated_at")
    if not raw:
        return None
    try:
        parsed = datetime.strptime(raw, _GENERATED_AT_FORMAT).replace(tzinfo=timezone.utc)
    except ValueError:
        return None
    delta = datetime.now(timezone.utc) - parsed
    return delta.total_seconds() / 60.0


def _render_age(minutes: float) -> str:
    seconds = minutes * 60.0
    if seconds <= 0:
        return "0s"
    if seconds < 60:
        return f"{int(seconds)}s"
    if minutes < 60:
        return f"{int(minutes)}m"
    hours = minutes / 60.0
    if hours < 24:
        return f"{int(hours)}h"
    return f"{int(hours / 24)}d"


def _cache_banner_line(cache_text: Optional[str]) -> str:
    try:
        now = datetime.now(timezone.utc).strftime(_GENERATED_AT_FORMAT)
    except Exception:
        return "── Orientation (RAM cache) ──\n"
    try:
        age = _cache_age_minutes(cache_text) if cache_text else None
    except Exception:
        age = None
    if age is None:
        return f"── Orientation (RAM cache) — age unknown (no parseable generated_at) · now {now} ──\n"
    return f"── Orientation (RAM cache) — regenerated {_render_age(age)} ago · now {now} ──\n"


def _orientation_staleness_grace_minutes(env: Mapping[str, Any]) -> float:
    raw = _env_get(env, "COORDINATOR_ORIENTATION_STALENESS_GRACE_MINUTES")
    if raw:
        try:
            return float(raw)
        except ValueError:
            pass  # malformed env override; fall through to the default below
    return float(_ORIENTATION_STALENESS_GRACE_MINUTES_DEFAULT)


def _orientation_staleness_drift_max_entries(env: Mapping[str, Any]) -> int:
    raw = _env_get(env, "COORDINATOR_ORIENTATION_STALENESS_DRIFT_MAX_ENTRIES")
    if raw:
        try:
            return int(raw)
        except ValueError:
            pass  # malformed env override; fall through to the default below
    return int(_ORIENTATION_STALENESS_DRIFT_MAX_ENTRIES_DEFAULT)


def _head_drift_is_small_boot(env: Mapping[str, Any], repo_root: Optional[str], cache_head: str) -> Optional[bool]:
    if not repo_root or not cache_head:
        return None
    path = Path(repo_root) / ".git" / "logs" / "HEAD"
    try:
        size = path.stat().st_size
        with path.open("rb") as handle:
            if size > _REFLOG_TAIL_WINDOW_BYTES:
                handle.seek(size - _REFLOG_TAIL_WINDOW_BYTES)
                handle.readline()
            chunk = handle.read()
    except Exception:
        return None

    positions = []
    for line in chunk.decode("utf-8", errors="replace").splitlines():
        parts = line.split(" ", 2)
        if len(parts) >= 2 and len(parts[1]) >= len(cache_head):
            positions.append(parts[1])
    if not positions:
        return None

    recent = list(dict.fromkeys(reversed(positions)))
    max_entries = _orientation_staleness_drift_max_entries(env)
    for depth, sha in enumerate(recent):
        if sha.startswith(cache_head):
            return depth <= max_entries
    return False


def _orientation_cache_staleness_banner(
    out: List[str], env: Mapping[str, Any], repo_root: Optional[str], cache_text: Optional[str]
) -> None:
    if _env_get(env, "COORDINATOR_ORIENTATION_STALENESS_OFF"):
        return
    if not cache_text:
        return

    cache_head = _extract_cache_head(cache_text)
    if not cache_head:
        out.append("\n")
        out.append(
            "── Orientation cache freshness UNVERIFIABLE — refresh: "
            f"{settings_home() / 'bin' / 'regenerate-orientation-cache'} --invoker workday-start ──\n"
        )
        return

    current_sha = _sha_boot(repo_root)
    if not current_sha:
        return
    if current_sha.startswith(cache_head):
        return

    age_minutes = _cache_age_minutes(cache_text)
    grace_minutes = _orientation_staleness_grace_minutes(env)
    if age_minutes is not None and age_minutes < grace_minutes:
        drift_is_small = _head_drift_is_small_boot(env, repo_root, cache_head)
        if drift_is_small is not False:
            return

    generated_at = _extract_cache_field(cache_text, "generated_at")
    short_head = current_sha[: len(cache_head)]
    cli = settings_home() / "bin" / "regenerate-orientation-cache"
    out.append("\n")
    out.append(
        f"── Orientation cache STALE: {cache_head}→{short_head} "
        f"({generated_at}) — refresh: {cli} --invoker workday-start ──\n"
    )


def _handle_cache_present_boot(out: List[str], cache_text: Optional[str]) -> bool:
    if cache_text is None:
        return False
    out.append("\n")
    out.append(_cache_banner_line(cache_text))
    out.append(cache_text)
    out.append("\n")
    out.append("── Orientation: 1 document loaded (from cache) ──\n")
    return True


def _lightweight_branch(out: List[str], repo_root: Optional[str]) -> None:
    out.append("\n")
    out.append("── Orientation (lightweight — /clear) ──\n")
    branch = _branch_boot(repo_root)
    if branch:
        out.append(f"  Branch: {branch}\n")
    out.append("  Full orientation available on next fresh session start.\n")


# ---------------------------------------------------------------------------
# handler
# ---------------------------------------------------------------------------


@register_op("hooks.project_orientation")
def _handler(params: dict, repo_root=None) -> dict:
    """SessionStart(startup|clear|compact): the `--lightweight` boot-path
    banner sequence, ported from the source script's `main()` --lightweight
    branch verbatim in ordering (module docstring: SCOPE FENCE).

    `payload_of(params)` carries `cwd` and `env` (a flat string->string
    mapping), reading either shape `params` reaches this handler in —
    wrapped as `params["payload"]` (the shape `warm/hook_http.py ::
    payload_from_event` builds) or flat, from the cold chain.
    Every banner below is independently fail-open, matching the source
    script's own per-call `try/except: pass` wrapping in `main()`.
    """
    payload = payload_of(params)

    env = payload.get("env")
    if not isinstance(env, Mapping):
        env = {}

    cwd = payload.get("cwd") or ""
    if not isinstance(cwd, str):
        cwd = ""

    session_repo_root = _resolve_repo_root(env, cwd)
    state_root = _resolve_state_root(session_repo_root)
    cache = Path(state_root) / "orientation_cache.md"

    cache_text: Optional[str] = None
    try:
        if cache.is_file():
            cache_text = cache.read_text(encoding="utf-8", errors="replace")
    except Exception:
        cache_text = None

    out: List[str] = []

    try:
        _repomap_staleness_banner(out, env, session_repo_root)
    except Exception:
        pass  # one optional banner failing must not block SessionStart output
    try:
        _exec_summary_staleness_banner(out, env, session_repo_root)
    except Exception:
        pass  # one optional banner failing must not block SessionStart output
    try:
        _peer_recheck_staleness_banner(out, env, session_repo_root)
    except Exception:
        pass  # one optional banner failing must not block SessionStart output
    try:
        _harness_version_drift_banner(out, env, session_repo_root)
    except Exception:
        pass  # one optional banner failing must not block SessionStart output
    try:
        _orientation_cache_staleness_banner(out, env, session_repo_root, cache_text)
    except Exception:
        pass  # one optional banner failing must not block SessionStart output
    try:
        _engine_resolution_banner(out)
    except Exception:
        pass  # one optional banner failing must not block SessionStart output
    try:
        _local_install_surface_banner(out, env, session_repo_root)
    except Exception:
        pass  # one optional banner failing must not block SessionStart output
    try:
        _install_currency_banner(out, env)
    except Exception:
        pass  # one optional banner failing must not block SessionStart output
    try:
        _corpus_currency_banner(out, env)
    except Exception:
        pass  # one optional banner failing must not block SessionStart output
    try:
        _tier_currency_banner(out, env, session_repo_root)
    except Exception:
        pass  # one optional banner failing must not block SessionStart output

    try:
        if _handle_cache_present_boot(out, cache_text):
            return context_only("SessionStart", "".join(out))
    except Exception:
        pass  # cache-present short-circuit is optional; fall through to lightweight branch

    try:
        _lightweight_branch(out, session_repo_root)
    except Exception:
        pass  # final optional banner failing must not block SessionStart output

    return context_only("SessionStart", "".join(out))
