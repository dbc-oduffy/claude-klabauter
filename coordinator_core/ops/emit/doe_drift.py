"""
coordinator_core.ops.emit.doe_drift — DoE-HEAD conformance fixture resolver + drift-check.

Purpose: resolve the DoE-HEAD emission-conformance fixture at check-time and run a
fail-loud drift-check when claude-klabauter's vendored schema-version lags the DoE-HEAD
``min_supported_contract_version``.  Implements the three-piece resolution contract
(dedicated ref + version band + shared provenance normalizer) ratified in
cross-repo/archive/2026-07-04-strang-emission-fixture-answers.md § Ask 1.

Resolution contract (DoE emission-conformance-contract.md § CD-1/CD-2):
  - **Body read — no origin.** Resolve DoE clone via machine-local ``repos.doe_claude``
    (direct TOML file read, NEVER the machine-local CLI — a plugin-load-PATH-only
    bootstrap hazard per CD-3 / Ask-2 caveat).  Read fixture body at
    ``coordinator/cockpit-contract/conformance/emission-conformance.json``.
    The registry directory itself is derived from ``coordinator_core._settings_home
    .machine_local_dir()`` — a pure env/home read, no subprocess — so the bootstrap
    -safety invariant (pre-PATH/CLI-availability) holds end to end: neither the
    directory derivation nor the file read shells out to anything (docs/plans/
    2026-07-11-coordinator-core-home-claude-read-repoint.md § C2/site 5).
  - **Freshness signal — dedicated ref.** Probe
    ``git ls-remote <origin> refs/tags/cockpit-contract-release``.  Record the SHA the
    vendored pin was cut at in ``_vendor/cockpit-contract/.doe-ref-pin``.  Fail loud on
    SHA mismatch (the tag has advanced since pin was cut).  **Degrade gracefully when
    the ref is absent** — DoE has not published it yet; treat as WARN/skip, not a
    hard failure (live constraint: ref absent at strang-02 pickup).
  - **Reader-first ahead-of-tag path (DR-203).** When origin_sha != pin_sha the module
    uses ``git merge-base --is-ancestor`` to decide direction before failing loud: if the
    release tag is an ancestor of the vendored pin (pin AHEAD of tag), the SHA difference
    is an expected reader-first re-vendor and emits ``AheadOfReleaseWarning`` rather than
    ``DriftError``.  Only when the pin is behind/diverged (or ancestry is indeterminate)
    does DriftError fire.  Indeterminate → DriftError is the fail-loud safe default.
  - **Version band — min_supported, not == HEAD.** Fail loud ONLY when claude-klabauter's pinned
    version < ``min_supported_contract_version`` from the fixture.  A ``contract_version``
    bump alone MUST NOT fire (reader-first mechanism).

Normalizer reuse: provenance normalization uses the EXISTING ``_normalize`` helper and
typed sentinels from ``coordinator_core.ops.emit.normalizers``.  The seven
AC5-PROVENANCE paths are scrubbed there; we do not duplicate the logic.

Negative-spec: DriftError is NOT raised when the release tag is a git-ancestor of the
vendored pin (reader-first window — pin AHEAD of tag is expected and graceful).  The
ancestry helper uses the DoE local clone only; it issues NO network call.

Spec backlink: state/handoffs/2026-07-04_201949_roadmap-strang-02.md (strang-02)
Spec backlink: docs/decisions/DR-210-claude-klabauter-native-tooling-ownership-strangler.md § 2
Oracle: /Users/example-operator/X/DoE-claude/coordinator/docs/wiki/emission-conformance-contract.md
"""

from __future__ import annotations

import json
import re
import subprocess
from coordinator_core.win_portability import (
    leaf_spawn_creationflags,
    no_console_creationflags,
)
import warnings
from pathlib import Path
from typing import Optional

from coordinator_core._settings_home import machine_local_dir, normalize_native_path
from coordinator_core.git_scope import (
    FOREIGN_REPO_GIT_TIMEOUT_SECONDS,
    PROBE_UNKNOWN,
    PROBE_YES,
    foreign_repo_unusable_reason,
    git_predicate,
    scoped_git_env,
)

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
# Review: code-reviewer (F9) — inline _EMIT_DIR; matches validate.py's style (_VENDOR_ROOT = Path(__file__).resolve().parent / "_vendor")
_VENDOR_CONTRACT = Path(__file__).resolve().parent / "_vendor" / "cockpit-contract"

# PIN_SHA_FILE records the cockpit-contract-release ref SHA at vendoring time.
# Written by the re-vendor step (bin/claude-klabauter-revendor-cockpit-contract.py); ``ABSENT`` when the ref was not
# yet published (live constraint at strang-02 pickup — DoE lands it in parallel).
PIN_SHA_FILE = _VENDOR_CONTRACT / ".doe-ref-pin"

# Path inside the DoE clone where the conformance fixture lives (CD-1).
_FIXTURE_REL = Path("coordinator/cockpit-contract/conformance/emission-conformance.json")

# Dedicated freshness ref DoE advances on every intentional contract change (CD-2 / AC8).
_CONTRACT_RELEASE_REF = "refs/tags/cockpit-contract-release"

# Machine-local registry file names (direct TOML read — never CLI).
#
# Bootstrap-safety invariant (load-bearing — do NOT repoint onto the `machine-local`
# CLI): resolve_doe_clone() below runs on the coordinator-root bootstrap path, before
# PATH/CLI availability is guaranteed.  `machine_local_dir()` (coordinator_core.
# _settings_home) is a PURE env/home read — no subprocess — so joining it here
# preserves the direct-TOML-file-read contract this module has always had; only the
# base directory derivation moved off the doomed `~/.claude/machine-local` literal
# onto the settings-home seam.  See docs/plans/2026-07-11-coordinator-core-home-claude
# -read-repoint.md § "bootstrap-safety invariant (load-bearing)" for the full
# reconciliation against machine-local-registry.md's general CLI-preferred doctrine
# (that doctrine governs generic peer-repo key reads, not this bootstrap path).
def _registry_paths() -> tuple[Path, Path]:
    """The local and base registry files, resolved PER CALL.

    NEVER bind these at module scope. `machine_local_dir()` derives from
    `_settings_home.settings_home()`, and under the resident warm engine this
    module's import outlives every caller it then serves -- a module-level
    constant freezes to whichever request happened to import it first, and
    every later caller reads that stranger's registry. `settings_home()`
    re-reads `os.environ` on every call precisely so a per-call reader is
    correct for free; see `warm/entry_seam.py :: _environ_identity_borrow`,
    which binds the caller's home for the life of one dispatch.
    """
    base = machine_local_dir()
    return base / "registry.local.toml", base / "registry.toml"

# Sentinel written to PIN_SHA_FILE when the ref was absent at vendor time.
_PIN_ABSENT_SENTINEL = "ABSENT"


# ---------------------------------------------------------------------------
# Errors + warnings
# ---------------------------------------------------------------------------

class DriftError(RuntimeError):
    """Raised when drift is detected: pinned version lags min_supported, or SHA mismatch.

    Negative-spec: DriftError is NOT raised when the ref is absent (ref-absent is a
    WARN/skip) or when ``contract_version`` alone advances (version-band gate, not
    equality gate).  Both of those conditions are graceful paths.
    """


class DriftWarning(UserWarning):
    """Emitted when a re-vendor action is urgently required but we do not hard-fail.

    Currently used when the pin file contains the ABSENT sentinel but DoE has already
    published the ``cockpit-contract-release`` ref on origin — the SHA-mismatch
    freshness gate is disabled until ``bin/claude-klabauter-revendor-cockpit-contract.py`` is re-run.

    Negative-spec: DriftWarning is NOT a DriftError — it does NOT abort execution.
    It is distinct from bare UserWarning so callers can filter or escalate it by
    category (``warnings.filterwarnings("error", category=DriftWarning)`` in tests).

    Review: code-reviewer (F1) — distinguishable from routine WARN/skip so the
    "pin ABSENT but ref now on origin" state cannot be silently ignored.
    """


class AheadOfReleaseWarning(UserWarning):
    """Emitted when the vendored pin is AHEAD of the cockpit-contract-release tag.

    Purpose: signal the expected reader-first window state — claude-klabauter has re-vendored a
    new contract version before DoE has advanced the ``cockpit-contract-release`` tag
    (per DR-203 reader-first invariant).  This is NOT a drift error; the SHA difference
    will self-heal when DoE advances the tag.

    Negative-spec: AheadOfReleaseWarning does NOT subclass DriftWarning.  It must not —
    ``bin/claude-klabauter-revendor-cockpit-contract.py``'s ``_post_vendor_drift_check`` kills on
    DriftWarning but only logs plain UserWarning.  AheadOfReleaseWarning ⊂ UserWarning,
    ⊄ DriftWarning → logged, not fatal → the re-vendor post-check passes in the
    reader-first window.
    """


class DoeResolveError(RuntimeError):
    """Raised when the DoE clone cannot be located via the machine-local registry."""


# ---------------------------------------------------------------------------
# Registry resolution — direct TOML file read (never CLI)
# ---------------------------------------------------------------------------

def _parse_toml_text(text: str) -> Optional[dict]:
    """Parse TOML text with tomllib (3.11+) or tomli, returning None on failure.

    Review: code-reviewer (F5) — separated from _read_toml_file so resolve_doe_clone
    can pre-read the file once and pass the text here, avoiding a second read_text()
    call when the TOML parse fails and the regex fallback is needed.
    """
    try:
        try:
            import tomllib as _tm  # type: ignore[import]  # stdlib 3.11+
        except ImportError:
            import tomli as _tm  # type: ignore[import,no-redef]  # third-party fallback
        return _tm.loads(text)
    except Exception:
        return None


def _read_toml_file(path: Path) -> Optional[dict]:
    """Parse a TOML file with tomllib (3.11+) or tomli, returning None on failure."""
    if not path.exists():
        return None
    return _parse_toml_text(path.read_text(encoding="utf-8"))


def _extract_repos_doe_claude_from_toml(data: dict) -> Optional[str]:
    """Walk the nested TOML dict looking for 'repos.doe_claude'."""
    # Two forms the registry CLI writes:
    #   1. Top-level quoted-dotted key: "repos.doe_claude" = "/path"
    #   2. Nested table: [repos] ... doe_claude = "/path"  (less common in this registry)
    # Check top-level flat dotted key first (most common form).
    for k, v in data.items():
        if k == "repos.doe_claude" and isinstance(v, str) and v:
            return v
    # Nested table form.
    repos = data.get("repos")
    if isinstance(repos, dict):
        val = repos.get("doe_claude")
        if isinstance(val, str) and val:
            return val
    return None


def _extract_repos_doe_claude_regex(text: str) -> Optional[str]:
    """Regex fallback for quoted-key form when TOML parser unavailable."""
    m = re.search(r'"repos\.doe_claude"\s*=\s*[\'"]([^\'"]+)[\'"]', text)
    if m:
        return m.group(1).strip()
    return None


def resolve_doe_clone() -> Path:
    """Resolve the DoE local clone root from the machine-local registry.

    Bootstrap-safe: reads ``registry.local.toml`` (per-machine) then ``registry.toml``
    (baseline) via DIRECT FILE READ.  NEVER calls the ``machine-local`` CLI — that
    binary is a plugin-load-PATH surface and is not available in cold shells (Ask-2
    caveat; DoE emission-conformance-contract.md § CD-3 / § Resolution Contract).
    Both file paths are joined off ``_settings_home.machine_local_dir()`` (pure
    env/home read, no subprocess) rather than a hardcoded ``~/.claude/machine-local``
    literal — the settings-home indirection does NOT reintroduce a CLI dependency.

    Raises DoeResolveError when the key is unset or the resolved path does not exist.
    """
    for registry_path in _registry_paths():
        if not registry_path.exists():
            continue
        # Review: code-reviewer (F5) — read once, pass text to both the TOML parser and
        # the regex fallback so the file is never read twice for the same path.
        text = registry_path.read_text(encoding="utf-8")
        data = _parse_toml_text(text)
        if data is not None:
            raw = _extract_repos_doe_claude_from_toml(data)
        else:
            # TOML parser unavailable — fall back to regex using already-read text.
            raw = _extract_repos_doe_claude_regex(text)
        if raw:
            candidate = normalize_native_path(raw).expanduser()
            if candidate.is_dir():
                return candidate

    raise DoeResolveError(
        "Cannot locate DoE clone: 'repos.doe_claude' unset or path absent in "
        f"{_registry_paths()[0]} and {_registry_paths()[1]}.  "
        "Set it with: machine-local set repos.doe_claude /path/to/DoE-claude"
    )


# ---------------------------------------------------------------------------
# Fixture body read (no network — always local clone HEAD)
# ---------------------------------------------------------------------------

def read_doe_fixture(doe_clone: Optional[Path] = None) -> dict:
    """Read and parse the DoE-HEAD conformance fixture body from the local clone.

    No network: this is a direct file read of the clone's HEAD on disk (CD-1).
    Resolves the DoE clone via ``resolve_doe_clone()`` when not supplied.

    Returns the parsed fixture dict containing ``contract_version``,
    ``min_supported_contract_version``, and all C2 entity arrays.

    Raises DoeResolveError when the DoE clone or fixture file cannot be found.

    Negative-spec: does NOT co-vendor the fixture — reading from the DoE clone
    at check-time is the anti-drift guarantee (DR-210 § 2).
    """
    doe_clone = doe_clone or resolve_doe_clone()
    fixture_path = doe_clone / _FIXTURE_REL
    if not fixture_path.exists():
        raise DoeResolveError(
            f"DoE conformance fixture not found at {fixture_path}.  "
            "DoE must commit it at coordinator/cockpit-contract/conformance/"
            "emission-conformance.json per CD-1."
        )
    try:
        return json.loads(fixture_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, ValueError) as exc:
        raise DoeResolveError(
            f"DoE conformance fixture is not valid JSON at {fixture_path}: {exc}"
        ) from exc


# ---------------------------------------------------------------------------
# Version-band check (CD-2)
# ---------------------------------------------------------------------------

def _parse_semver(version: str) -> tuple[int, int, int]:
    """Parse a SemVer string to a comparable (major, minor, patch) int triple.

    Pads to length 3 with zeros so two-component versions ("2.5") compare equal to
    their three-component equivalents ("2.5.0") — without padding, (2, 5) < (2, 5, 0)
    in Python, which would incorrectly fire DriftError for semantically equal versions.

    Review: code-reviewer (F4) — pad to length 3 to prevent two-component version
    strings from comparing as less than their three-component semantic equivalents.
    """
    try:
        parts = [int(x) for x in version.split(".")]
        padded = (parts + [0, 0, 0])[:3]
        return (padded[0], padded[1], padded[2])
    except (ValueError, AttributeError) as exc:
        raise DriftError(
            f"Cannot parse version string {version!r} as SemVer: {exc}"
        ) from exc


def check_version_band(pinned_version: str, fixture: dict) -> None:
    """Fail loud when ``pinned_version < min_supported_contract_version`` in the fixture.

    Gate semantics (CD-2 / AC_Q-a-band):
      - ``pinned >= min_supported``           → PASS (re-vendor advisory only)
      - ``pinned < min_supported``            → FAIL LOUD (re-vendor required)
      - ``pinned == contract_version``        → PASS (no equality gate)
      - ``pinned != contract_version``        → PASS (version band, not equality)

    Negative-spec: a ``contract_version`` bump alone MUST NOT trigger this gate.
    The equality-fail-loud pattern is architecturally incompatible with reader-first
    (DoE emission-conformance-contract.md § CD-2 / Gate semantics).
    """
    min_supported_str = fixture.get("min_supported_contract_version")
    if not min_supported_str:
        # Missing field — cannot check. Warn and pass (DoE commitment: field must be present).
        warnings.warn(
            "DoE fixture missing 'min_supported_contract_version' — cannot run version-band "
            "check; treating as PASS.  DoE must publish this field per CD-2.",
            stacklevel=2,
        )
        return
    pinned_t = _parse_semver(pinned_version)
    min_t = _parse_semver(min_supported_str)
    if pinned_t < min_t:
        raise DriftError(
            f"claude-klabauter vendored schema-version {pinned_version} is below the DoE "
            f"min_supported_contract_version {min_supported_str}.  Re-vendor the "
            "cockpit-contract pin (python3 bin/claude-klabauter-revendor-cockpit-contract.py) to satisfy the version-band gate "
            "(DR-210 § 2 / strang-02 AC_Q-a-band)."
        )


# ---------------------------------------------------------------------------
# Freshness probe (dedicated ref, bounded ls-remote)
# ---------------------------------------------------------------------------

def _read_pin_sha() -> Optional[str]:
    """Read the cockpit-contract-release SHA recorded at vendor time.

    Returns None when the pin file is absent or contains the ABSENT sentinel
    (ref was not yet published when vendoring).

    Negative-spec: ``ABSENT`` is a graceful path, not an error.  The ref may not
    be published yet (live constraint at strang-02 pickup).
    """
    if not PIN_SHA_FILE.exists():
        return None
    raw = PIN_SHA_FILE.read_text(encoding="utf-8").strip()
    if not raw or raw == _PIN_ABSENT_SENTINEL:
        return None
    return raw


def probe_freshness_ref(doe_clone: Path) -> Optional[str]:
    """Probe DoE origin for the cockpit-contract-release ref SHA via git ls-remote.

    Returns the **commit** SHA when the ref exists on origin, or None when absent.
    A missing ref means DoE has not published it yet (ref-absent graceful path).

    Network: one bounded single-ref ls-remote call (no full fetch, no fixture body).
    After the network call a local ``rev-parse <sha>^{}`` is attempted to dereference
    the SHA to its underlying commit: for annotated tags, ls-remote returns the
    tag-object SHA; ``rev-parse ^{}`` peels it to the commit SHA that merge-base
    requires.  For lightweight tags, ``rev-parse ^{}`` is a no-op (same SHA).
    If the tag object is absent from the local clone, the raw ls-remote SHA is
    returned as a graceful fallback (no regression vs pre-existing behaviour).

    Invariant: when the tag object is locally present, this function returns a commit
    SHA (never a tag-object SHA), ensuring both the SHA-match path and the
    ``_tag_is_ancestor_of_pin`` path receive a value merge-base can operate on.

    Raises DriftError only on subprocess failure unrelated to ref absence.

    Negative-spec: returning None (ref absent) is NOT the same as DriftError.
    Ref-absent → caller warns/skips; SHA mismatch → caller raises DriftError.

    Negative-spec (git scoping): ``doe_clone`` is a DIFFERENT repository from the
    one this process runs in, and ``git -C`` does not scope to it on its own — an
    inherited ``GIT_DIR`` (git exports one to every hook it runs, often as a
    relative ``"."``) still wins over discovery. Unscoped, the ``remote get-url
    origin`` below returns the LOCAL repo's origin, the ls-remote then finds no
    cockpit-contract-release there, and this function returns None — reported
    downstream as "DoE has not published it yet", a confident false claim about
    somebody else's remote. Both local calls therefore run under
    ``git_scope.scoped_git_env()``, and the clone is confirmed reachable AS a
    repository (git-dir confined to its own tree) before either runs. See
    ``coordinator_core/git_scope.py`` for the 2026-08-03 incident.

    Review: code-reviewer (F1) — dereference via local rev-parse ^{} (reviewer-approved
    alternative) so annotated tags yield a commit SHA rather than a tag-object SHA.
    merge-base returns exit 128 ("Not a commit") on a tag-object SHA, silently breaking
    the AheadOfReleaseWarning path when cockpit-contract-release is an annotated tag
    (standard git release practice).  --dereference was the preferred flag but is not
    available on Apple git 2.x; rev-parse ^{} is semantically equivalent when the tag
    object is locally present.
    """
    unusable = foreign_repo_unusable_reason(doe_clone)
    if unusable is not None:
        warnings.warn(
            f"DoE clone at {doe_clone} could not be read as a git repository "
            f"({unusable}) — skipping freshness check; this is not a finding "
            "about what DoE has or has not published.",
            stacklevel=3,
        )
        return None

    try:
        origin = subprocess.run(
            ["git", "-C", str(doe_clone), "remote", "get-url", "origin"],
            capture_output=True,
            text=True,
            check=False,
            timeout=FOREIGN_REPO_GIT_TIMEOUT_SECONDS,
            env=scoped_git_env(),
            **no_console_creationflags(),
        )
    except FileNotFoundError as exc:
        raise DriftError(
            "git executable not found — cannot probe DoE freshness ref.  "
            "Install git to enable the freshness check."
        ) from exc
    except subprocess.TimeoutExpired:
        warnings.warn(
            f"Reading DoE origin URL in {doe_clone} exceeded "
            f"{FOREIGN_REPO_GIT_TIMEOUT_SECONDS}s; skipping freshness check. A local "
            "`git remote get-url` over budget is a defect in this probe, not a reason "
            "to wait longer.",
            stacklevel=3,
        )
        return None

    if origin.returncode != 0:
        warnings.warn(
            f"Cannot determine DoE origin URL for freshness probe: {origin.stderr.strip()}; "
            "skipping freshness check.",
            stacklevel=3,
        )
        return None

    origin_url = origin.stdout.strip()
    if not origin_url:
        warnings.warn(
            "DoE clone has no git remote 'origin' — skipping freshness check.",
            stacklevel=3,
        )
        return None

    try:
        ls_remote = subprocess.run(
            ["git", "ls-remote", origin_url, _CONTRACT_RELEASE_REF],
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
            **no_console_creationflags(),
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        warnings.warn(
            f"git ls-remote failed for DoE origin {origin_url!r}: {exc}; "
            "skipping freshness check.",
            stacklevel=3,
        )
        return None

    if ls_remote.returncode != 0:
        warnings.warn(
            f"git ls-remote returned non-zero for DoE origin: {ls_remote.stderr.strip()}; "
            "skipping freshness check.",
            stacklevel=3,
        )
        return None

    output = ls_remote.stdout.strip()
    if not output:
        # Ref absent on origin — DoE hasn't published it yet (live constraint).
        return None

    # ls-remote output: "<sha>\t<refname>"
    raw_sha = output.split()[0]

    # Dereference to a commit SHA via local rev-parse.  For annotated tags,
    # ls-remote returns the tag-object SHA; merge-base operates on commits and
    # will error (exit 128, "Not a commit") on a tag-object SHA.  ``rev-parse
    # <sha>^{}`` peels any tag object to the commit it points to; for lightweight
    # tags (and plain commits) it is a no-op, returning the same SHA.
    # This requires the tag object to be present in the local clone (expected for
    # a maintained DoE clone); if the object is absent, we fall back to raw_sha
    # (same behaviour as before this fix — no regression vs the pre-existing path).
    #
    # Review: code-reviewer (F1) — prefer local rev-parse ^{} over the
    # --dereference ls-remote flag because Apple git 2.x does not expose
    # --dereference on ls-remote; the reviewer's finding explicitly listed
    # rev-parse ^{} as an equivalent alternative.
    try:
        rev_parse = subprocess.run(
            ["git", "-C", str(doe_clone), "rev-parse", f"{raw_sha}^{{}}"],
            capture_output=True,
            text=True,
            check=False,
            timeout=FOREIGN_REPO_GIT_TIMEOUT_SECONDS,
            env=scoped_git_env(),
            **leaf_spawn_creationflags(),
        )
        if rev_parse.returncode == 0:
            deref_sha = rev_parse.stdout.strip()
            if deref_sha:
                return deref_sha
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass  # git absent or timed out — fall through to raw_sha

    return raw_sha


def _tag_is_ancestor_of_pin(
    doe_clone: Path,
    tag_sha: str,
    pin_sha: str,
) -> Optional[bool]:
    """Check whether tag_sha is a git-ancestor of pin_sha in the DoE clone.

    Purpose: distinguish "reader-first re-vendor — pin AHEAD of release tag" (graceful)
    from "pin stale behind current tag" (must re-vendor) using local-only ancestry probe.
    No network call; operates entirely on the DoE local clone's object database.

    Returns:
      True  — exit 0: tag_sha IS an ancestor of pin_sha (pin is AHEAD of tag).
      False — exit 1: not an ancestor (pin is behind or on a diverged branch).
      None  — any other exit code, git not found, timeout, or commits absent from the
              local clone.  Indeterminate → caller treats as DriftError (fail-loud safe
              default: cannot prove the pin is ahead → treat as drift).

    Negative-spec: this helper issues NO network call and does NOT advance any ref.
    It is mockable at the function level for unit tests.

    Negative-spec (git scoping): the 0/1/other tri-state AND the stripped
    repo-scoping environment that makes ``-C`` actually scope to ``doe_clone``
    both come from ``git_scope.git_predicate`` — see that module for why
    ``returncode != 0`` here would conflate "definitely not an ancestor" with
    "the question never reached DoE's object database".
    """
    verdict, _reason = git_predicate(
        doe_clone,
        ["merge-base", "--is-ancestor", tag_sha, pin_sha],
    )
    if verdict == PROBE_UNKNOWN:
        # Exit code >= 2 means git error (commits not present, not a git repo,
        # environment-retargeted probe, etc.) — indeterminate, never False.
        return None
    return verdict == PROBE_YES


def check_freshness(doe_clone: Optional[Path] = None) -> None:
    """Check the freshness signal against the vendored-pin SHA.

    Resolution (CD-2):
      1. Read the pin SHA from ``_vendor/cockpit-contract/.doe-ref-pin``.
         - Real SHA → proceed to origin probe.
         - Absent or ABSENT sentinel → still probe origin to distinguish two states:
             (A) Ref absent on origin too → WARN/skip (DoE hasn't published yet).
             (B) Ref present on origin but pin not updated → emit DriftWarning (action
                 required: re-vendor to arm the gate).
      2. Probe DoE origin: ``git ls-remote <origin> refs/tags/cockpit-contract-release``.
         - Ref absent on origin → WARN and skip (DoE hasn't published yet — graceful).
         - Ref present + SHA matches pin → PASS.
         - Ref present + SHA differs from pin → ancestry check (step 3).
      3. Ancestry check via ``_tag_is_ancestor_of_pin`` (local, no network):
         - True (tag is ancestor of pin, pin AHEAD): emit ``AheadOfReleaseWarning`` and
           return — reader-first window; the discrepancy is expected and will self-heal
           when DoE advances the tag.
         - False or None (pin behind/diverged or indeterminate): raise ``DriftError``
           preserving the "SHA mismatch" message text — re-vendor required.  Indeterminate
           → DriftError is the fail-loud safe default.

    Negative-spec: ref-absent on origin is NEVER a DriftError.  DriftError fires ONLY on
    confirmed SHA mismatch when the pin is NOT ahead (strang-02 AC_Q-a-sha).
    AheadOfReleaseWarning is NOT a DriftWarning — it does NOT cause the re-vendor
    post-check to die (DR-203 reader-first invariant).

    Review: code-reviewer (F1) — when pin contains ABSENT, still probe origin.  If the
    ref has appeared on origin, emit DriftWarning (not DriftError — hard-fail the moment
    DoE publishes would break claude-klabauter before re-vendor can run; a loud, distinct,
    category-distinguishable warning is the safer choice that preserves operations while
    demanding action).  The two states — "ref absent on origin" vs "ref present but pin
    not updated" — are now distinguishable.
    """
    pin_sha = _read_pin_sha()
    if pin_sha is None:
        # Pin is ABSENT.  Probe origin to distinguish state (A) vs state (B).
        # If the DoE clone cannot be resolved (DoeResolveError), degrade gracefully.
        try:
            resolved_clone = doe_clone or resolve_doe_clone()
        except DoeResolveError:
            warnings.warn(
                "No cockpit-contract-release SHA recorded in vendor pin "
                f"({PIN_SHA_FILE}) and DoE clone unavailable for origin probe — "
                "freshness check skipped.  "
                "Run python3 bin/claude-klabauter-revendor-cockpit-contract.py to populate the pin when DoE publishes the ref.",
                stacklevel=2,
            )
            return

        origin_sha = probe_freshness_ref(resolved_clone)

        if origin_sha is not None:
            # State (B): DoE published the ref, but re-vendor hasn't been run.
            # Emit a LOUD, category-distinct DriftWarning — not DriftError, because a hard
            # failure the instant DoE publishes would break claude-klabauter before
            # bin/claude-klabauter-revendor-cockpit-contract.py runs.
            warnings.warn(
                "[ACTION REQUIRED] DoE has published the cockpit-contract-release ref "
                f"(SHA={origin_sha!r}) but the vendored pin ({PIN_SHA_FILE}) still "
                "contains the ABSENT sentinel — the SHA-mismatch freshness gate is "
                "disabled.  Re-vendor via python3 bin/claude-klabauter-revendor-cockpit-contract.py to arm the gate and enable "
                "drift detection (strang-02 AC_Q-a-sha / DR-210 § 2).",
                DriftWarning,
                stacklevel=2,
            )
        else:
            # State (A): ref also absent on origin — DoE hasn't published yet.
            warnings.warn(
                "No cockpit-contract-release SHA recorded in vendor pin "
                f"({PIN_SHA_FILE}) — freshness check skipped.  "
                "Pin was likely cut before DoE published the ref (expected during initial "
                "strang-02 window).  Re-vendor when the ref is published to enable the check.",
                stacklevel=2,
            )
        return

    doe_clone = doe_clone or resolve_doe_clone()
    origin_sha = probe_freshness_ref(doe_clone)

    if origin_sha is None:
        # Ref absent on DoE origin — graceful skip (live constraint).
        warnings.warn(
            f"DoE origin has no '{_CONTRACT_RELEASE_REF}' ref — "
            "DoE has not published the dedicated freshness ref yet.  "
            "Freshness check skipped (graceful ref-absent path per strang-02 live constraint).",
            stacklevel=2,
        )
        return

    if origin_sha != pin_sha:
        # Ancestry check: if the release tag is an ancestor of our pin, the pin is
        # AHEAD of the tag (reader-first window).  If not — or if indeterminate — treat
        # as stale drift and fail loud.
        is_ahead = _tag_is_ancestor_of_pin(doe_clone, origin_sha, pin_sha)
        if is_ahead is True:
            warnings.warn(
                f"[READER-FIRST] Vendored pin SHA={pin_sha!r} is AHEAD of the "
                f"cockpit-contract-release tag SHA={origin_sha!r}.  "
                "This is expected during the DR-203 reader-first re-vendor window — claude-klabauter "
                "has vendored the new contract version before DoE has advanced the release "
                "tag.  The discrepancy will self-heal when DoE runs the tag-advance step.  "
                "No action required until the release tag catches up.",
                AheadOfReleaseWarning,
                stacklevel=2,
            )
            return
        raise DriftError(
            f"DoE cockpit-contract-release ref SHA mismatch: "
            f"vendored-pin SHA={pin_sha!r}, DoE origin SHA={origin_sha!r}.  "
            "The contract has advanced since this pin was cut.  "
            "Re-vendor the cockpit-contract (python3 bin/claude-klabauter-revendor-cockpit-contract.py) to update the pin."
        )
    # SHA match — freshness confirmed.


# ---------------------------------------------------------------------------
# Full drift-check entry point
# ---------------------------------------------------------------------------

def run_drift_check(
    pinned_version: Optional[str] = None,
    doe_clone: Optional[Path] = None,
) -> dict:
    """Run the full DoE-HEAD drift-check and return the parsed conformance fixture.

    Steps (strang-02 Spec items 1/2/3):
      1. Resolve DoE clone via machine-local registry (body read — no origin).
      2. Read the DoE-HEAD conformance fixture body.
      3. check_version_band: fail loud when pinned_version < min_supported.
      4. check_freshness: probe the dedicated ref; warn on absent, fail on mismatch.

    Returns the parsed fixture dict (convenience: callers that also need the fixture
    for normalization/comparison do not need a second read_doe_fixture() call).

    Negative-spec: this function reads ONE file and issues ONE network probe.  It does
    NOT re-vendor the pin, does NOT write any files, and does NOT touch rag's store.
    """
    from coordinator_core.ops.emit.validate import read_schema_version

    doe_clone = doe_clone or resolve_doe_clone()
    fixture = read_doe_fixture(doe_clone)

    if pinned_version is None:
        pinned_version = read_schema_version()

    check_version_band(pinned_version, fixture)
    check_freshness(doe_clone)

    return fixture
