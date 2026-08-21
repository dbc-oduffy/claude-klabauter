"""
coordinator_core.ops.deliverable_rollup — JSON-RPC "deliverable.rollup" COMPUTE_ONLY operation.

Purpose: Given a ``deliverable_id`` wire parameter, scans the deliverable-spine read-model
(plan frontmatter, stub handoff frontmatter) for artifacts carrying that id, then unions
their non-null ``initiative`` FKs and resolves each to its ``state/initiatives/<id>.yaml``
entry. Returns structured fields only — no prose is composed here; DoE owns the render.

The scan surface covers five paths:
  docs/plans/*.md          — primary; deliverable_id + initiative FK co-occur here most
                             (plans mint the id and most often carry the initiative FK).
  state/handoffs/*.md      — secondary; stub handoffs may carry deliverable_id.
  archive/handoffs/**/*.md — archived stubs; same scan.
  archive/specs/**/*.md    — archived plans. `fleet.archive_completed_plans` moves a plan
                             from docs/plans/ to archive/specs/<YYYY-MM>/ the moment its
                             status flips terminal; excluding this root would un-resolve
                             every shipped deliverable's own plan the instant it ships.
  state/sizings/*.yaml     — sizing objects (C10, whole-document YAML, no frontmatter
                             fence — read via `_read_sizing_yaml`, not the markdown parser).

Resolution semantics: DIRECT only (slice-1). Each artifact's own ``initiative`` frontmatter
FK is the forward-edge. Transitive resolution (DAG walk via ``blocks``/``blocked_by`` edges)
is NOT performed — it resolved 0 deliverables against the live tree (C0 findings) and carries
the false-transitive-edge risk retired by ``commit.anchors`` §6.1.

Keying scope: common_dir — handler receives repo_root = git_common_dir (the .git directory).
The main worktree root is derived via main_worktree_root(repo_root) from
coordinator_core/ops/fleet/_common.py (standard-layout assumption, documented there).
DO NOT scan from repo_root directly — repo_root is the .git common dir, not the worktree root.
Scanning from .git/state/ instead of <worktree>/state/ is a silent scan-miss; always derive
the worktree from main_worktree_root(repo_root) before constructing any scan path.

Self-registration: importing this module calls register_op("deliverable.rollup", _handler)
as a side-effect. Add this module to coordinator_core/ops/__init__.py to trigger registration
at start_server() time.

Spec backlink: pln-claude-klabauter-deliverable-spine-fact--cd004e § C2
Producer contract: coordinator_core/contract/deliverable-rollup-producer-contract.md

Negative-spec (hard-won):
  - ZERO git subprocess — HARD INVARIANT. This op reads on-disk frontmatter and YAML only.
    It has no staged-index reads to perform. The ``commit.anchors`` git-subprocess carve-out
    (read-only git show / git diff --cached) does NOT apply here and does NOT transfer.
    A single non-git, env-miss-only machine-local registry subprocess may fire once per
    process (memoized) to resolve the claude-klabauter central-state root; it neither mutates state
    nor flips COMPUTE_ONLY. Any GIT subprocess is a violation of this op's classification.
    The single non-git, env-miss-only memoized machine-local registry root-resolution
    subprocess is an explicit non-mutating carve-out preserving COMPUTE_ONLY.
  - Does NOT write any git object, any file under state/, archive/, tasks/, or any other
    coordinator substrate path. COMPUTE_ONLY per DR-208 classification.
  - Does NOT write any temp file that a caller reads back.
  - Does NOT call assemble_roadmap_dag — that helper is roadmap_id-keyed and cannot be
    driven from a deliverable_id without a prior scan. Wrong surface for this op.
  - Does NOT walk the ``blocks``/``blocked_by`` DAG (transitive resolution is NOT slice-1).
  - Does NOT use ctx.repo_root — it is None in the global service; uses the repo_root arg.
  - deliverable_id from the wire param is used ONLY as a frontmatter filter VALUE. It is
    NEVER used as a filesystem path component, directory fragment, or open() argument.
    Malformed or injected values (e.g. ``../``, absolute paths, embedded nulls) return
    safe-empty, not an error — the token simply will not match any frontmatter value.
  - Does NOT apply the commit.anchors omit-on-multi ambiguity rule: N > 1 artifacts per
    deliverable is the EXPECTED grain (a deliverable spans multiple plans/stubs by design).
    The op AGGREGATES initiative FKs across all matching artifacts; omit-on-multi would be
    a category error here.
  - Empty advances_initiatives is the safe null (not an error). ~96% of deliverables
    today return an empty list (C0 recall envelope). Do NOT treat an empty list as a warning.
"""

from __future__ import annotations

import logging
import os
import subprocess
from coordinator_core.win_portability import no_console_creationflags
import sys
from pathlib import Path
from typing import Dict, List, Optional, Set

from coordinator_core._settings_home import settings_home
from coordinator_core.dag import _parse_frontmatter, _read_meta
from coordinator_core.engine_root import coordinator_engine_root_env
from coordinator_core.ipc import register_op
from coordinator_core.ops.emit.sections.initiatives import _simple_yaml_load
from coordinator_core.ops.fleet._common import main_worktree_root
from coordinator_core.telemetry import op_latency

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Machine-local registry helpers (copied from queue_append.py by convention;
# cross-module shared-helper extraction is out of scope — see improvement-queue).
# ---------------------------------------------------------------------------

_MACHINE_LOCAL_IMPL_ENV = "MACHINE_LOCAL_IMPL"
_CLAUDE_HOME_ENV = "CLAUDE_HOME"
# Review: code-reviewer — subprocess timeout bound so a hung registry script
# does not block the thread indefinitely (at most once per process; still bounded).
_MACHINE_LOCAL_TIMEOUT = 5  # seconds


def _claude_home() -> str:
    """Return the ~/.claude root, honouring CLAUDE_HOME env var for test isolation."""
    override = os.environ.get(_CLAUDE_HOME_ENV)
    if override:
        return override
    return os.path.join(os.path.expanduser("~"), ".claude")


def _machine_local_impl() -> str:
    """Return the path to _machine_local.py, honouring MACHINE_LOCAL_IMPL for tests."""
    # TODO(33cf462): de-dup _claude_home/_machine_local_impl into shared module
    # (queued: state/improvement-queue/2026-07-06-claude-klabauter-root-shared-helper-extraction.yaml)
    override = os.environ.get(_MACHINE_LOCAL_IMPL_ENV)
    if override:
        return override
    settings_home_impl = os.path.join(settings_home(), "bin", "_machine_local.py")
    if os.path.exists(settings_home_impl):
        return settings_home_impl
    return os.path.join(_claude_home(), "bin", "_machine_local.py")


def _machine_local_get(key: str) -> Optional[str]:
    """Call ``machine-local get <key>`` and return the value, or None on failure."""
    impl = _machine_local_impl()
    try:
        result = subprocess.run(
            [sys.executable, impl, "get", key],
            capture_output=True,
            text=True,
            timeout=_MACHINE_LOCAL_TIMEOUT,
            **no_console_creationflags(),
        )
    except (OSError, subprocess.TimeoutExpired):
        print(f"skip: _machine_local_get: result = subprocess.run( failed: {sys.exc_info()[1]}", file=sys.stderr)
        return None
    if result.returncode != 0 or not result.stdout.strip():
        return None
    return result.stdout.strip()


def _claude_klabauter_root() -> Optional[str]:
    """Resolve the claude-klabauter repo root.

    Resolution chain:
        1. ``COORDINATOR_ENGINE_ROOT`` env var (via the accessor) — trusted
           as-is, but ONLY when this process is the one the caller ran in
           (see below).
        2. ``machine-local get repos.claude_klabauter``.
        3. Returns None when unresolvable; callers degrade gracefully (WARN+skip).

    The engine-root env var is a property of a CALLING process. Under the warm
    engine this op executes in a long-lived server process whose environment
    was inherited from whichever session happened to spawn it, so trusting the
    raw read under warm serving would name the SPAWNER's root rather than the
    current caller's — the write exits 0, prints a normal path, and lands
    nowhere the caller can see (see ``queue_append._output_root_override``'s
    docstring for the same hazard on the sibling op). ``execution_route() ==
    IN_PROCESS`` is true for every non-server process, so the env var stays
    honoured everywhere except the served route, which falls through to the
    machine-local registry lookup instead (correct in both routes, since it
    resolves the true repo root rather than a caller-scoped override).

    Spec backlink: pln-stop-the-rot-claude-klabauter-state-home-placement-4cc787 § AC13
    """
    override = (coordinator_engine_root_env(__name__) or "").strip()
    if override and op_latency.execution_route() == op_latency.IN_PROCESS:
        # Review: code-reviewer — expand ~ and shell vars so users setting
        # COORDINATOR_ENGINE_ROOT=~/X/... get the correct absolute path
        # instead of a literal tilde that won't resolve.
        return os.path.expanduser(os.path.expandvars(override))
    val = _machine_local_get("repos.claude_klabauter")
    return val if val else None


# ---------------------------------------------------------------------------
# Central initiatives directory resolver — process-scope memoization
# ---------------------------------------------------------------------------

# Module-level memoization state for _central_initiatives_dir.
# "Not yet resolved" → _CENTRAL_ROOT_RESOLVED is False.
# "Resolved (or confirmed unresolvable)" → _CENTRAL_ROOT_RESOLVED is True.
# _RESOLVED_CENTRAL_ROOT is None iff the root was unresolvable (fallback branch).
_RESOLVED_CENTRAL_ROOT: Optional[Path] = None
_CENTRAL_ROOT_RESOLVED: bool = False
_CENTRAL_ROOT_WARNED: bool = False


def _reset_central_root_cache() -> None:
    """Test-only helper: clear the ``_central_initiatives_dir`` process-scope memo.

    The memo's "at most once per process lifetime (AC10)" contract is correct under
    the spawn-per-call execution model, where the process exits after one op. It is
    NOT correct under pytest, where every test shares one interpreter: the first test
    to resolve pins the central root (and the once-per-process WARN) for the whole
    session, so later tests silently skip the resolution branch and pass or fail on
    a neighbour's state rather than their own.

    Mirrors ``coordinator_core.liveness._reset_live_ids_cache``. Wired into the
    suite-root autouse reset in ``coordinator_core/conftest.py``; the module-local
    fixture in ``ops/tests/test_deliverable_rollup.py`` calls it too (it previously
    poked the three globals directly).
    """
    global _RESOLVED_CENTRAL_ROOT, _CENTRAL_ROOT_RESOLVED, _CENTRAL_ROOT_WARNED
    _RESOLVED_CENTRAL_ROOT = None
    _CENTRAL_ROOT_RESOLVED = False
    _CENTRAL_ROOT_WARNED = False


def _central_initiatives_dir(worktree_root: Path) -> Path:
    """Return the initiatives directory for this process, resolving central-state root.

    Purpose: resolves the claude-klabauter central-state root (COORDINATOR_ENGINE_ROOT env → machine-local
    registry → worktree-local fallback) and returns the initiatives directory path.
    The resolved central root is memoized at module scope so the registry subprocess
    fires at most once per process lifetime (AC10).

    Resolution precedence:
        1. COORDINATOR_ENGINE_ROOT env var — fast path, subprocess-free.
        2. machine-local get repos.claude_klabauter — non-git subprocess, env-miss only.
        3. Fallback to worktree-local state/initiatives/ — preserves today's behavior
           for claude-klabauter's own worktree and un-provisioned environments. Emits a WARN
           once per process on this branch (systemic misconfiguration observable, AC9).
           WARN is NOT emitted for the coincident-dir case (claude-klabauter's own worktree where
           central root resolves and happens to equal the worktree root).

    Spec backlink: pln-stop-the-rot-claude-klabauter-state-home-placement-4cc787 § AC13
    """
    global _RESOLVED_CENTRAL_ROOT, _CENTRAL_ROOT_RESOLVED, _CENTRAL_ROOT_WARNED

    if not _CENTRAL_ROOT_RESOLVED:
        root = _claude_klabauter_root()
        if root is not None:
            _RESOLVED_CENTRAL_ROOT = Path(root)
        # else: remains None → fallback branch below
        _CENTRAL_ROOT_RESOLVED = True

    if _RESOLVED_CENTRAL_ROOT is not None:
        return _RESOLVED_CENTRAL_ROOT / "state" / "initiatives"

    # Fallback: claude-klabauter root was unresolvable.
    # Concurrent-first-call window: two asyncio.to_thread threads can both read
    # _CENTRAL_ROOT_WARNED=False before either sets it to True, causing WARN to fire twice
    # in the startup window. This is benign/best-effort under CPython's GIL — the once-per-
    # process invariant is sequential, not atomic across threads.
    if not _CENTRAL_ROOT_WARNED:
        logger.warning(
            "deliverable.rollup: claude-klabauter central-state root is unresolvable "
            "(COORDINATOR_ENGINE_ROOT unset and machine-local registry lookup returned None). "
            "Falling back to worktree-local state/initiatives/ — systemic "
            "misconfiguration may cause initiative FKs to resolve empty for "
            "non-claude-klabauter worktrees. Set COORDINATOR_ENGINE_ROOT or configure repos.claude_klabauter "
            "in the machine-local registry to resolve centrally."
        )
        _CENTRAL_ROOT_WARNED = True

    return worktree_root / "state" / "initiatives"


# ---------------------------------------------------------------------------
# Empty payload builder (safe-null shape for error / unknown-deliverable cases)
# ---------------------------------------------------------------------------

def _empty_payload(deliverable_id: str = "", scan_incomplete: bool = False) -> dict:
    """Return a well-formed, contract-compliant empty payload.

    Purpose: single home for the safe-null shape so every early-return path
    produces an identical, schema-valid response. Used when repo_root is None,
    deliverable_id is absent, or when no artifacts match.

    scan_incomplete defaults to False (no scan was attempted, or the scan that
    was attempted completed fully). Callers that reach this after a scan pass
    the scan's actual result through rather than accepting the default.
    """
    return {
        "deliverable_id": deliverable_id,
        "resolution_mode": "direct",
        "artifacts_matched": 0,
        "advances_initiatives": [],
        "scan_incomplete": scan_incomplete,
    }


# ---------------------------------------------------------------------------
# Shared resolvable-root surface (C10, docs/plans/2026-08-13-spec-backlinks-
# cite-a-stable-deliverable-id.md). ONE constant so this scanner and
# coordinator_core.ops.spec_backlink_resolve.build_index cannot drift into two
# hard-coded root lists that happen to agree. Each entry is
# (relative_path_parts, "flat" | "recursive") — "flat" scans base_dir/* only
# (iterdir()), "recursive" walks base_dir/** (os.walk()). "flat" roots hold
# either *.md (frontmatter-fenced) or *.yaml/*.yml (whole-document, the
# state/sizings/ root only) — see `_scan_artifacts_by_deliverable_id`'s
# per-root reader dispatch.
#
# LEG (a) of C10 CLEARED (cross-repo/inbox/2026-08-13-doe-claude-em-spec-
# backlink-id-form-ruled-and-rollup-cleared.md): the reader
# (coordinator_render_rollup.py) is count-agnostic over `artifacts_matched`
# and, since the finish-strangler port, claude-klabauter-resident — no DoE-side change
# or sign-off was required. `SIZINGS_ONLY_ROOT` is folded directly into this
# tuple; `_scan_artifacts_by_deliverable_id` now scans all five roots,
# including `state/sizings/*.yaml` via a YAML-capable collector (mirrors
# `spec_backlink_resolve._index_sizings_dir`'s existing implementation).
SIZINGS_ONLY_ROOT: tuple[tuple[str, ...], str] = (("state", "sizings"), "flat")

RESOLVABLE_ARTIFACT_ROOTS: tuple[tuple[tuple[str, ...], str], ...] = (
    (("docs", "plans"), "flat"),
    (("state", "handoffs"), "flat"),
    (("archive", "handoffs"), "recursive"),
    (("archive", "specs"), "recursive"),
    SIZINGS_ONLY_ROOT,
)


# ---------------------------------------------------------------------------
# Sizing-object reader (whole-document YAML, no frontmatter fence)
# ---------------------------------------------------------------------------


def _read_sizing_yaml(path: Path) -> dict:
    """Read a `state/sizings/*.yaml` record. Returns {} on any error.

    Sizings are whole-document YAML (no `---` frontmatter fence) — reading
    them via `_read_meta`'s fence-scanning parser would silently return {}
    (correctly, but for the wrong reason: it never finds a closing fence).
    This reader is a same-shape twin of
    `spec_backlink_resolve._read_sizing_yaml` (same rationale: no runtime
    coupling between this module and a peer chunk's file — that module
    already imports FROM this one for the shared root constants, so the
    reverse import would be circular).
    """
    try:
        import yaml
    except ImportError:
        logger.debug("deliverable.rollup: PyYAML unavailable; cannot read %s", path)
        return {}
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            data = yaml.safe_load(fh)
    except OSError as exc:
        logger.debug("deliverable.rollup: could not read %s: %s", path, exc)
        return {}
    except Exception as exc:  # noqa: BLE001 — malformed YAML degrades to empty, not a crash
        logger.debug("deliverable.rollup: could not parse %s: %s", path, exc)
        return {}
    return data if isinstance(data, dict) else {}


# ---------------------------------------------------------------------------
# Artifact scanner — collects frontmatter dicts for a given deliverable_id
# ---------------------------------------------------------------------------


def _scan_artifacts_by_deliverable_id(
    worktree_root: Path,
    deliverable_id: str,
) -> tuple[List[dict], bool]:
    """Scan the five-path surface and return frontmatter/sizing dicts for matching artifacts.

    Purpose: collects every artifact (plan, handoff, or sizing object) whose
    ``deliverable_id`` field equals the queried value. The five scan paths are:
      - docs/plans/*.md          (primary: plans mint deliverable_id + initiative FK)
      - state/handoffs/*.md      (secondary: stub handoffs)
      - archive/handoffs/**/*.md (archived stubs)
      - archive/specs/**/*.md    (archived plans — fleet.archive_completed_plans moves a
                                  plan here from docs/plans/ the instant its status flips
                                  terminal, so excluding this root un-resolves every
                                  shipped deliverable's own plan)
      - state/sizings/*.yaml     (sizing objects — whole-document YAML, no frontmatter
                                  fence; read via `_read_sizing_yaml`, not `_read_meta`)

    deliverable_id is used ONLY as a comparison value against the parsed frontmatter field —
    it is NEVER used as a path component. Scan roots are hard-coded constants.

    NOTE: uses os.walk(onerror=...)/iterdir(), NOT Path.glob()/rglob() — glob()'s
    selector silently swallows PermissionError while walking (an unreadable dir/subtree
    yields an empty iterator, no exception), which would make a blocked scan root
    indistinguishable from "deliverable has no artifacts" — exactly the silent
    roll-up-to-nothing bug this guards against. docs/plans and state/handoffs are
    flat (iterdir()); archive/handoffs and archive/specs are recursive
    (os.walk(onerror=...)).

    Returns (matches, scan_incomplete):
      matches         — list of frontmatter dicts (one per matching artifact). An empty
                        list means no artifact in the scan surface carries the queried id
                        (when scan_incomplete is also False — see below).
      scan_incomplete — True when any scan root could not be fully enumerated (permission
                        denied). Callers MUST treat True as "this result may be missing
                        artifacts", not as "these are genuinely all the artifacts" — the
                        entire point of this signal is to make a blocked scan distinguishable
                        from a genuinely-empty deliverable.
    """
    matches: List[dict] = []
    scan_incomplete = False

    def _collect(path: Path) -> None:
        try:
            fm = _read_meta(str(path))
        except Exception as exc:  # noqa: BLE001 — parity with initiatives.collect quarantine
            logger.debug("deliverable.rollup: could not read frontmatter from %s: %s", path, exc)
            return
        if not fm:
            return
        # deliverable_id used only as a comparison VALUE — no path construction.
        artifact_did = fm.get("deliverable_id")
        if isinstance(artifact_did, str):
            if artifact_did.strip() == deliverable_id:
                matches.append(fm)
                return
        # plan_id match arm (C10b leg (c), docs/plans/2026-08-13-spec-backlinks-
        # cite-a-stable-deliverable-id.md): compares the SAME `deliverable_id`
        # argument against the artifact's own `plan_id` field.
        # `pln-` and `dlv-` mint prefixes are disjoint by construction
        # (bin/mint-deliverable-id / bin/mint-plan-id), so a correctly-minted
        # `dlv-`-shaped query can never match a correctly-minted plan_id
        # value and vice versa. This module's invariance test proves the arm
        # is a no-op for every correctly-minted `pln-`-prefixed plan_id
        # value against every `dlv-`-shaped query deliverable.rollup's own
        # handler can produce — it does NOT cover a hand-authored or
        # otherwise malformed plan_id value that happens to carry a `dlv-`
        # prefix (frontmatter is not attacker-controlled in this system, so
        # this is treated as low practical risk rather than handled).
        artifact_pid = fm.get("plan_id")
        if isinstance(artifact_pid, str) and artifact_pid.strip() == deliverable_id:
            matches.append(fm)

    def _collect_sizing(path: Path) -> None:
        doc = _read_sizing_yaml(path)
        if not doc:
            return
        # Same match-arm shape as `_collect` above, applied to a
        # whole-document YAML dict instead of a frontmatter dict — sizings
        # are the one root whose records are not markdown-fenced.
        artifact_did = doc.get("deliverable_id")
        if isinstance(artifact_did, str):
            if artifact_did.strip() == deliverable_id:
                matches.append(doc)
                return
        artifact_pid = doc.get("plan_id")
        if isinstance(artifact_pid, str) and artifact_pid.strip() == deliverable_id:
            matches.append(doc)

    sizings_parts, _sizings_kind = SIZINGS_ONLY_ROOT
    sizings_dir = worktree_root.joinpath(*sizings_parts)

    # Flat scan roots (from the shared RESOLVABLE_ARTIFACT_ROOTS constant).
    # state/sizings/ is whole-document YAML (*.yaml/*.yml, no frontmatter
    # fence) — every other flat root is markdown frontmatter (*.md).
    flat_dirs = [
        worktree_root.joinpath(*parts)
        for parts, kind in RESOLVABLE_ARTIFACT_ROOTS
        if kind == "flat"
    ]
    for base_dir in flat_dirs:
        if not base_dir.is_dir():
            continue
        try:
            entries = list(base_dir.iterdir())
        except OSError as exc:
            logger.warning(
                "deliverable.rollup: cannot scan %s — %s; scan marked incomplete "
                "(deliverable roll-up may be missing artifacts under this root)",
                base_dir,
                exc,
            )
            scan_incomplete = True
            continue
        is_sizings_root = base_dir == sizings_dir
        for path in entries:
            if not path.is_file():
                continue
            if is_sizings_root:
                if path.suffix in (".yaml", ".yml"):
                    _collect_sizing(path)
            elif path.suffix == ".md":
                _collect(path)

    # Recursive scan roots (from the shared RESOLVABLE_ARTIFACT_ROOTS constant).
    recursive_dirs = [
        worktree_root.joinpath(*parts)
        for parts, kind in RESOLVABLE_ARTIFACT_ROOTS
        if kind == "recursive"
    ]
    for archive_dir in recursive_dirs:
        if not archive_dir.is_dir():
            continue
        walk_errors: List[OSError] = []
        for dirpath, _dirnames, filenames in os.walk(archive_dir, onerror=walk_errors.append):
            for fn in filenames:
                if fn.endswith(".md"):
                    path = Path(dirpath) / fn
                    if path.is_file():
                        _collect(path)
        if walk_errors:
            for exc in walk_errors:
                logger.warning(
                    "deliverable.rollup: cannot walk %s — %s; scan marked incomplete "
                    "(deliverable roll-up may be missing artifacts under this root)",
                    getattr(exc, "filename", archive_dir),
                    exc,
                )
            scan_incomplete = True

    return matches, scan_incomplete


# ---------------------------------------------------------------------------
# Initiative resolver — resolves a single initiative FK to its YAML record
# ---------------------------------------------------------------------------


def _resolve_initiative(
    initiatives_dir: Path,
    initiative_id: str,
) -> Optional[Dict[str, Optional[str]]]:
    """Resolve a single initiative FK to its label/status from state/initiatives/<id>.yaml.

    Purpose: reads and parses the YAML file at <initiatives_dir>/<initiative_id>.yaml.
    Returns None when the file does not exist or cannot be parsed (precision-over-recall:
    an unresolvable FK is omitted from advances_initiatives, not guessed).

    initiative_id is used ONLY to construct <initiatives_dir>/<id>.yaml via
    ``initiatives_dir / (initiative_id + ".yaml")`` — the initiatives_dir is a
    controlled path derived from the worktree root, not from any wire token.
    """
    # Review: code-reviewer — guard against path-traversal in frontmatter-sourced initiative_id.
    # A value like "../../evil" would produce a path outside state/initiatives/ even with
    # is_file() as defence-in-depth; reject any id containing a path separator or leading '.'.
    if not initiative_id or "/" in initiative_id or "\\" in initiative_id or initiative_id.startswith("."):
        logger.debug(
            "deliverable.rollup: initiative_id %r fails traversal guard; omitting.",
            initiative_id,
        )
        return None

    yaml_path = initiatives_dir / f"{initiative_id}.yaml"
    if not yaml_path.is_file():
        return None

    try:
        content = yaml_path.read_text(encoding="utf-8")
        fm = _simple_yaml_load(content)
    except Exception as exc:  # noqa: BLE001
        logger.debug(
            "deliverable.rollup: could not read/parse initiative file %s: %s",
            yaml_path,
            exc,
        )
        return None

    label = fm.get("label")
    status = fm.get("status")

    return {
        "id": initiative_id,
        "label": label if isinstance(label, str) and label else None,
        "status": status if isinstance(status, str) and status else None,
    }


# ---------------------------------------------------------------------------
# Op handler
# ---------------------------------------------------------------------------


@register_op("deliverable.rollup")
def _handler(
    params: dict,
    repo_root: Optional[Path] = None,
) -> dict:
    """JSON-RPC "deliverable.rollup" handler — COMPUTE_ONLY, sync.

    Purpose: scans the deliverable-spine read-model for artifacts carrying the queried
    ``deliverable_id``, unions their non-null ``initiative`` FKs, resolves each to its
    ``state/initiatives/<id>.yaml`` entry, and returns structured fields. No prose is
    composed; no state is written.

    Sync (not async): all I/O is synchronous (disk reads plus the single memoized
    non-git machine-local registry-resolution subprocess on env-miss).
    ipc.py offloads sync handlers via asyncio.to_thread, keeping the event loop live.

    Params:
        deliverable_id  (str) — the deliverable id to look up. Used ONLY as a frontmatter
                                filter value; never a filesystem path component.

    Returns the structured payload per the producer contract
    (coordinator_core/contract/deliverable-rollup-producer-contract.md §1):
        {
          "deliverable_id": "<echoed>",
          "resolution_mode": "direct",
          "artifacts_matched": <int>,
          "advances_initiatives": [{"id": ..., "label": ..., "status": ...}],
          "scan_incomplete": <bool>
        }

    Returns a well-formed empty payload (artifacts_matched=0, advances_initiatives=[])
    when repo_root is None, deliverable_id is absent/empty, or no artifacts match.
    Empty advances_initiatives is the safe null and the COMMON case today (~96% of
    deliverables have no initiative FK set — C0 recall envelope).

    scan_incomplete: _scan_artifacts_by_deliverable_id also returns whether a scan
    root (docs/plans, state/handoffs, or archive/handoffs) could not be fully
    enumerated (e.g. permission-denied); a WARNING is logged naming the blocked
    root. That signal is on the wire as of DoE's be8b5d88 reader-widen (their
    render layer appends " (partial scan)" per rendered line when it is set) —
    it is emitted here as an explicit bool on every payload, including the
    safe-empty shapes.

    COMPUTE_ONLY affirmation (DR-208 five questions):
        1. No git write executed — zero git subprocess of any kind (hard invariant).
        2. No file written under state/, archive/, tasks/, or any coordinator substrate path.
        3. No temp file written that a caller reads back.
        4. Returns computed structured data only; no mutation of any kind.
        5. Zero read-only git subprocesses — this op has no staged-index reads;
           the commit.anchors git-subprocess carve-out does NOT apply here.

    Keying scope: common_dir — repo_root IS the .git directory (git_common_dir result).
    Worktree is derived via main_worktree_root(repo_root) — never from repo_root.parent
    inline. Scanning from .git/state/ instead of <worktree>/state/ is a silent scan-miss.
    """
    deliverable_id: str = params.get("deliverable_id") or ""
    deliverable_id = deliverable_id.strip()

    if repo_root is None:
        logger.warning(
            "deliverable.rollup: repo_root is None — cannot derive worktree; "
            "returning empty payload. This is expected only in unit-test stubs; "
            "ensure the engine supplies repo_root (common_dir) in production."
        )
        return _empty_payload(deliverable_id)

    if not deliverable_id:
        logger.warning(
            "deliverable.rollup: deliverable_id param is absent or empty; returning empty payload."
        )
        return _empty_payload("")

    worktree_root = main_worktree_root(repo_root)

    # ------------------------------------------------------------------
    # 1. Collect all artifacts carrying the queried deliverable_id.
    # ------------------------------------------------------------------
    matching_artifacts, scan_incomplete = _scan_artifacts_by_deliverable_id(
        worktree_root, deliverable_id
    )
    artifacts_matched = len(matching_artifacts)

    if scan_incomplete:
        # Per-scan-root WARNINGs (naming the specific blocked directory) were already
        # logged inside _scan_artifacts_by_deliverable_id; this one names the affected
        # deliverable_id, for a reader grepping by id rather than by path.
        logger.warning(
            "deliverable.rollup: scan for deliverable_id=%r was incomplete (a scan "
            "root could not be fully enumerated) — this payload may be missing "
            "artifacts/initiatives; scan_incomplete=True is included on the wire "
            "for this response",
            deliverable_id,
        )

    if artifacts_matched == 0:
        # Unknown deliverable — safe-empty is the correct response, not an error.
        # scan_incomplete=True means this "0 matches" may instead be a blocked scan
        # root — the WARNING already logged inside _scan_artifacts_by_deliverable_id
        # is today's only signal of that; scan_incomplete is on the wire as of DoE's
        # be8b5d88 reader-widen, so it is passed through here rather than dropped.
        # Review: code-reviewer — use _empty_payload to avoid dual maintenance of the safe-null shape.
        return _empty_payload(deliverable_id, scan_incomplete=scan_incomplete)

    # ------------------------------------------------------------------
    # 2. UNION non-null initiative FKs across all matching artifacts.
    #    AC3: N > 1 artifacts is the EXPECTED grain — AGGREGATE, do not omit.
    #    Dedup by initiative id. Omit only an individual edge that is null
    #    or unresolvable (precision-over-recall at the EDGE level only).
    # ------------------------------------------------------------------
    initiatives_dir = _central_initiatives_dir(worktree_root)

    seen_ids: Set[str] = set()  # Review: code-reviewer — parameterize consistent with file-wide typing convention
    advances_initiatives: List[Dict[str, Optional[str]]] = []

    for fm in matching_artifacts:
        initiative_fk = fm.get("initiative")
        if not isinstance(initiative_fk, str) or not initiative_fk.strip():
            # Null or absent FK — omit this edge (precision-over-recall at edge level).
            continue

        initiative_id = initiative_fk.strip()
        if initiative_id in seen_ids:
            # Already resolved and included — dedup by id.
            continue

        resolved = _resolve_initiative(initiatives_dir, initiative_id)
        if resolved is None:
            # FK present but resolves to no real file — omit (precision-over-recall).
            logger.debug(
                "deliverable.rollup: initiative FK %r on deliverable %r does not resolve "
                "to a real state/initiatives/<id>.yaml file; omitting edge.",
                initiative_id,
                deliverable_id,
            )
            continue

        seen_ids.add(initiative_id)
        advances_initiatives.append(resolved)

    return {
        "deliverable_id": deliverable_id,
        "resolution_mode": "direct",
        "artifacts_matched": artifacts_matched,
        "advances_initiatives": advances_initiatives,
        "scan_incomplete": scan_incomplete,
    }
