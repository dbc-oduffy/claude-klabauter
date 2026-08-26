"""
test_engine_root_conformance.py — consumes DoE-claude's engine-root
conformance fixture as the fidelity oracle for claude-klabauter's two DR-132
two-tier engine-root ladder implementations.

Drives BOTH:
  - the shim's single implementation,
    ``coordinator/lib/resolve-claude-klabauter/_resolve_claude_klabauter.py::
    resolve_claude_klabauter_root_with_class()``, and
  - C4a's wrapper, ``coordinator_core.engine_root::
    coordinator_engine_root_with_class()``,
against every declarative case in DoE's fixture, and asserts both agree
with each other and with the fixture's ``expect``.

Spec backlink:
    docs/plans/2026-08-07-two-tier-engine-root-adopt-dr132.md (chunk C8)
    DoE-claude coordinator/engine-root-contract/conformance/
        engine-root-conformance.json (read live, never vendored — see
        negative-spec below and that fixture's own schema docstring on
        pull-fresh-never-vendored)
    DoE-claude coordinator/hooks/scripts/_engine_root.py
        (resolve_claude_klabauter_root_with_class, _resolve_live_working_tree,
        LIVE_TREE_ENV_VARS)

Negative-spec: this file must NEVER embed a copy of the fixture's cases
(paths/registry bodies/expectations) — it reads the fixture live off the
DoE checkout, resolved via ``machine-local get
engine.working_repos.doe_claude``, at collection time. Referencing case
IDs (kebab-case strings) in the xfail-reason table below is parametrize
plumbing, not a vendored copy of fixture content.

THE KNOWN LADDER DIVERGENCE (see module-level ``_XFAIL_ENV_RUNG_REASON``
table): claude-klabauter's live-tree ladder has NO env-var rungs at all (registry
key then ``.claude-klabauter-live-root`` sentinel only); DoE's ``_resolve_live_working_tree``
checks ``LIVE_TREE_ENV_VARS = (REPO_CLAUDE_KLABAUTER, CLAUDE_KLABAUTER_ROOT)`` first.
C4a's wrapper adds a CLAUDE_KLABAUTER_ROOT-only rung (not REPO_CLAUDE_KLABAUTER). Cases
that fail ONLY because of this are marked ``xfail(strict=True)`` — see the
table for the exact case-by-case reasoning. A case failing for ANY other
reason is left to fail for real; do not paper over it.
"""
from __future__ import annotations

import importlib.util
import json
import types
from pathlib import Path
from typing import Any, Optional

import pytest

import coordinator_core.engine_root as engine_root_mod
from coordinator_core.machine_resolver import registry_get


def _machine_local_get(key: str) -> Optional[str]:
    """Zero-spawn registry read (DR-071, 2026-07-22): the direct-tomllib
    reader every DoE-root anchor consumer now binds ``repos.doe_claude``
    reads to, in preference to the ``machine-local`` CLI. Kept as a thin
    named wrapper (rather than calling ``registry_get`` inline at each call
    site) so the module-scope ``_resolve_doe_root()`` call below stays
    zero-spawn without a spawn-shaped call node anywhere in this file.
    """
    return registry_get(key)


def _resolve_doe_root() -> Optional[Path]:
    value = _machine_local_get("engine.working_repos.doe_claude")
    if not value:
        return None
    p = Path(value)
    return p if p.is_dir() else None


_DOE_ROOT = _resolve_doe_root()

_SKIP_REASON = (
    "engine.working_repos.doe_claude is unregistered or its path is absent — "
    "register the DoE checkout via 'machine-local set "
    "engine.working_repos.doe_claude <path>' to run the DoE conformance oracle. "
    "claude-klabauter carries no CI lane where this suite runs rather than skips "
    "(no .github/workflows/, per CLAUDE.md § Build & Test) — this skip is a "
    "known, unremedied gap, not a claim of coverage."
)


def _load_fixture(doe_root: Path) -> dict:
    """Load the fixture live off *doe_root*. Never returns a skip — a
    missing/unparseable fixture on a box where the prerequisite key IS
    registered is a FAILURE (AC12), the degenerate case that would
    otherwise read green-by-skip forever on a dev box."""
    fixture_path = (
        doe_root
        / "coordinator"
        / "engine-root-contract"
        / "conformance"
        / "engine-root-conformance.json"
    )
    if not fixture_path.is_file():
        raise AssertionError(
            f"DoE checkout is registered at {doe_root} but the conformance "
            f"fixture is missing at {fixture_path} — this is a FAILURE, not a "
            "skip (AC12)."
        )
    try:
        with open(fixture_path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        raise AssertionError(
            f"conformance fixture at {fixture_path} is unparseable: {exc}"
        ) from exc

    schema_version = data.get("schema_version")
    if not isinstance(schema_version, str) or not schema_version.startswith("1."):
        raise AssertionError(
            f"conformance fixture schema_version is {schema_version!r}, expected a "
            "1.x.y value — a MAJOR bump means this driver no longer understands the "
            "fixture's shape; update the driver before re-running rather than "
            "silently continuing to drive cases against a shape it predates."
        )
    return data


def _sibling_engine_checkout_absent(doe_root: Path) -> bool:
    """Recompute DoE's rung-3 sibling-walk expression against DoE's own
    module — never a hardcoded path (that would be wrong on every other
    host). Per the fixture's ``preconditions`` schema entry: read-only load
    of DoE's ``_engine_root.py`` by path, purely to evaluate
    ``Path(__file__).resolve().parents[3].parent / _CLAUDE_KLABAUTER_SIBLING_DIR_NAME``
    exactly as DoE's own rung 3 would."""
    module_path = doe_root / "coordinator" / "hooks" / "scripts" / "_engine_root.py"
    spec = importlib.util.spec_from_file_location(
        "_doe_engine_root_precondition_probe", module_path
    )
    assert spec is not None and spec.loader is not None
    mr = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mr)
    sibling = Path(mr.__file__).resolve().parents[3].parent / mr._CLAUDE_KLABAUTER_SIBLING_DIR_NAME
    return not sibling.is_dir()


_NO_ENV_RUNGS_REASON_TEMPLATE = (
    "{cid}: claude-klabauter's live-tree ladder "
    "(coordinator/lib/resolve-claude-klabauter/_resolve_claude_klabauter.py::_resolve_claude_klabauter_root) has "
    "NO env-var rungs at all — registry key 'repos.claude_klabauter' then the "
    "'.claude-klabauter-live-root' sentinel only. DoE's _resolve_live_working_tree "
    "(coordinator/hooks/scripts/_engine_root.py:527-560) checks "
    "LIVE_TREE_ENV_VARS = (REPO_CLAUDE_KLABAUTER, CLAUDE_KLABAUTER_ROOT) first. C4a's wrapper "
    "(coordinator_core/engine_root.py::coordinator_engine_root_with_class) adds a "
    "CLAUDE_KLABAUTER_ROOT-only short-circuit rung, not REPO_CLAUDE_KLABAUTER. Out of scope for "
    "this chunk per its own brief — do not add env rungs to force conformance."
)

#: Case IDs that fail ONLY because of the named env-rung divergence above.
#: Root-caused by re-deriving each case's ladder path by hand against both
#: `_resolve_claude_klabauter.py` and `engine_root.py` — see this chunk's run-report
#: sidecar for the full per-case derivation.
_XFAIL_ENV_RUNG_REASON: dict[str, str] = {
    "no-published-engine-non-working-repo-resolves-live-tree": _NO_ENV_RUNGS_REASON_TEMPLATE.format(
        cid="no-published-engine-non-working-repo-resolves-live-tree"
    ),
    "working-repo-with-published-engine-still-resolves-live-tree": _NO_ENV_RUNGS_REASON_TEMPLATE.format(
        cid="working-repo-with-published-engine-still-resolves-live-tree"
    )
    + " AMENDED 2026-08-19 (C5, docs/plans/2026-08-19-an-engine-root-is-a-"
    "stamped-build.md; see the C9 DoE memo): this reason's original trailing "
    "clause said both entrypoints fall through to the published-engine "
    "last-resort branch, returning resolved-engine. That consequence is "
    "stale post-C5 — the fixture creates no "
    "<KLABAUTER>/coordinator_core/_engine_stamp, so the stamp check now "
    "denies the published root too, and both entrypoints raise "
    "ClaudeKlabauterResolutionError instead. The env-rung cause above is unchanged.",
    # Backfilled 2026-08-17. These two arrived with DoE's rung-0 work
    # (`cd0d8bf38` added the rung and the first case, `742821db9` the second),
    # which landed 2026-08-08 — a day AFTER this table was authored in
    # `a00a6e864`, so they were never added to it. Both fixture cases set
    # `REPO_CLAUDE_KLABAUTER` with `CLAUDE_KLABAUTER_ROOT: null`, and claude-klabauter reads neither
    # rung on this path (the shim has no env rungs; the C4a wrapper's rung is
    # `CLAUDE_KLABAUTER_ROOT`-only), so they fail for EXACTLY the divergence above and
    # nothing else — verified against the live fixture, not inferred from the
    # failure text. `strict=True` keeps this honest: give claude-klabauter a
    # `REPO_CLAUDE_KLABAUTER` rung and these XPASS and fail loudly.
    "explicit-env-override-beats-registered-published-engine": _NO_ENV_RUNGS_REASON_TEMPLATE.format(
        cid="explicit-env-override-beats-registered-published-engine"
    )
    + " AMENDED 2026-08-19 (C5, docs/plans/2026-08-19-an-engine-root-is-a-"
    "stamped-build.md; see the C9 DoE memo): this reason's original trailing "
    "clause said both entrypoints fall through to the published-engine "
    "last-resort branch, returning resolved-engine. That consequence is "
    "stale post-C5 — the fixture creates no "
    "<KLABAUTER>/coordinator_core/_engine_stamp, so the stamp check now "
    "denies the published root too, and both entrypoints raise "
    "ClaudeKlabauterResolutionError instead. The env-rung cause above is unchanged.",
    "engine-target-readable-still-beaten-by-healthy-rung0-override": _NO_ENV_RUNGS_REASON_TEMPLATE.format(
        cid="engine-target-readable-still-beaten-by-healthy-rung0-override"
    )
    + " AMENDED 2026-08-19 (C5, docs/plans/2026-08-19-an-engine-root-is-a-"
    "stamped-build.md; see the C9 DoE memo): this reason's original trailing "
    "clause said both entrypoints fall through to the published-engine "
    "last-resort branch, returning resolved-engine. That consequence is "
    "stale post-C5 — the fixture creates no "
    "<KLABAUTER>/coordinator_core/_engine_stamp, so the stamp check now "
    "denies the published root too, and both entrypoints raise "
    "ClaudeKlabauterResolutionError instead. The env-rung cause above is unchanged.",
    "undeterminable-session-root-resolves-live-tree-never-diverts": _NO_ENV_RUNGS_REASON_TEMPLATE.format(
        cid="undeterminable-session-root-resolves-live-tree-never-diverts"
    )
    + " AMENDED 2026-08-19 (C5, docs/plans/2026-08-19-an-engine-root-is-a-"
    "stamped-build.md; see the C9 DoE memo): this reason's original trailing "
    "clause said both entrypoints fall through to the published-engine "
    "last-resort branch instead of the live tree. That consequence is stale "
    "post-C5 — the fixture creates no "
    "<KLABAUTER>/coordinator_core/_engine_stamp, so the stamp check now "
    "denies the published root too, and both entrypoints raise "
    "ClaudeKlabauterResolutionError instead. The env-rung cause above is unchanged.",
    "empty-string-working-repos-declarations-gate-none": _NO_ENV_RUNGS_REASON_TEMPLATE.format(
        cid="empty-string-working-repos-declarations-gate-none"
    )
    + " No published engine is registered in this case either, so both "
    "entrypoints raise ClaudeKlabauterResolutionError instead of resolving the live tree.",
    "working-repos-merge-across-both-registry-files": _NO_ENV_RUNGS_REASON_TEMPLATE.format(
        cid="working-repos-merge-across-both-registry-files"
    )
    + " AMENDED 2026-08-19 (C5, docs/plans/2026-08-19-an-engine-root-is-a-"
    "stamped-build.md; see the C9 DoE memo): this reason's original trailing "
    "clause said both entrypoints fall through to the published-engine "
    "last-resort branch instead of the live tree. That consequence is stale "
    "post-C5 — the fixture creates no "
    "<KLABAUTER>/coordinator_core/_engine_stamp, so the stamp check now "
    "denies the published root too, and both entrypoints raise "
    "ClaudeKlabauterResolutionError instead. The env-rung cause above is unchanged.",
    "half-installed-klabauter-missing-coordinator-core": _NO_ENV_RUNGS_REASON_TEMPLATE.format(
        cid="half-installed-klabauter-missing-coordinator-core"
    )
    + " No usable published engine either (half-installed-clone guard), so both "
    "entrypoints raise ClaudeKlabauterResolutionError instead of resolving the live tree.",
    "registered-klabauter-root-does-not-exist": _NO_ENV_RUNGS_REASON_TEMPLATE.format(
        cid="registered-klabauter-root-does-not-exist"
    )
    + " No usable published engine either (root does not exist), so both "
    "entrypoints raise ClaudeKlabauterResolutionError instead of resolving the live tree.",
    "live-tree-env-var-precedence-repo-claude-klabauter-first": _NO_ENV_RUNGS_REASON_TEMPLATE.format(
        cid="live-tree-env-var-precedence-repo-claude-klabauter-first"
    )
    + " This case additionally breaks the single-implementation invariant AC10 "
    "checks for: the shim (no env rungs at all) raises, while the wrapper's "
    "CLAUDE_KLABAUTER_ROOT-only rung short-circuits and returns CLAUDE_KLABAUTER_ROOT's value "
    "(<OTHER_REPO> in this case) rather than REPO_CLAUDE_KLABAUTER's — the two "
    "entrypoints disagree with EACH OTHER, not just with DoE's expectation.",
    # Added 2026-08-20 (session fc8834f1) on doe-claude-em's request, landed
    # with the fixture in their `fa0bfb6d9`. Reproduced here before entering it
    # rather than taken on their report.
    "coordinator-engine-root-beats-claude-klabauter-live-root-when-both-set": _NO_ENV_RUNGS_REASON_TEMPLATE.format(
        cid="coordinator-engine-root-beats-claude-klabauter-live-root-when-both-set"
    )
    + " NOTE THE DISCRIMINANT THIS CASE PINS IS CONFORMANT, and that is what "
    "separates this entry from every other one in this table: the wrapper "
    "returns (<LIVE_TREE>, 'live-working-tree') — DoE's expectation exactly — "
    "so COORDINATOR_ENGINE_ROOT's precedence over CLAUDE_KLABAUTER_ROOT is verified, not "
    "waived. What fails is the shim==wrapper equality assertion above it: the "
    "shim, having no env rungs at all, never sees either name and raises "
    "ClaudeKlabauterResolutionError. Same divergence as "
    "'live-tree-env-var-precedence-repo-claude-klabauter-first', which fails "
    "identically. `strict=True` is load-bearing here — give the shim an env "
    "rung and this XPASSes loudly, which is the signal that the C4a wrapper's "
    "carve-out can retire.",
}


#: C5 (docs/plans/2026-08-19-an-engine-root-is-a-stamped-build.md): "an
#: engine root is a stamped build. No stamp, no engine." claude-klabauter's
#: `_resolve_published_engine` now ALSO requires a valid engine build stamp
#: (`coordinator_core/_engine_stamp`, non-empty) before treating
#: `repos.claude_klabauter` as usable. No case in DoE's fixture creates
#: `<KLABAUTER>/coordinator_core/_engine_stamp` (`existing_dirs` lists stop
#: one level short, at `<KLABAUTER>/coordinator_core`), so this stamp check
#: makes `_resolve_published_engine` return `None` for EVERY fixture case —
#: these four currently-PASSING cases expect `resolved-engine` and now get a
#: raised `ClaudeKlabauterResolutionError` instead. Re-derived from an actual test
#: run against the live fixture, not from prose — see the plan's § Cross-repo
#: CORRECTION block for why an earlier draft's "seven cases move" claim was
#: wrong and must not be re-cited. C9's DoE memo names this table and asks
#: DoE to ship stamped-klabauter fixture cases; `strict=True` below forces
#: removal of each entry the moment that lands and the case starts passing
#: for real.
_STAMP_RULE_REASON_TEMPLATE = (
    "{cid}: C5 (docs/plans/2026-08-19-an-engine-root-is-a-stamped-build.md; "
    "see the C9 DoE memo) added a stamp check to "
    "coordinator/lib/resolve-claude-klabauter/_resolve_claude_klabauter.py::_resolve_published_engine "
    "— 'an engine root is a stamped build. No stamp, no engine.' This "
    "fixture case creates no <KLABAUTER>/coordinator_core/_engine_stamp, so "
    "the published root is no longer 'usable' and both entrypoints raise "
    "ClaudeKlabauterResolutionError instead of returning resolved-engine as this "
    "case expects."
)

_XFAIL_STAMP_RULE_REASON: dict[str, str] = {
    "confirmed-non-working-repo-with-published-engine-resolves-engine": (
        _STAMP_RULE_REASON_TEMPLATE.format(
            cid="confirmed-non-working-repo-with-published-engine-resolves-engine"
        )
    ),
    "step4-last-resort-published-engine-when-live-rung-misses": (
        _STAMP_RULE_REASON_TEMPLATE.format(
            cid="step4-last-resort-published-engine-when-live-rung-misses"
        )
    ),
    "engine-target-readable-diverts-working-repo": (
        _STAMP_RULE_REASON_TEMPLATE.format(
            cid="engine-target-readable-diverts-working-repo"
        )
    ),
    "unhealthy-rung0-override-falls-through-to-published-engine": (
        _STAMP_RULE_REASON_TEMPLATE.format(
            cid="unhealthy-rung0-override-falls-through-to-published-engine"
        )
    ),
}


def _substitute(value: str, tokens: dict[str, str]) -> str:
    for token, real in tokens.items():
        value = value.replace(token, real)
    return value


def _prepare_case(
    case: dict[str, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> dict[str, str]:
    """Materialize *case*'s filesystem/env/session-repo setup under
    *tmp_path*, returning the token -> real-path substitution map used to
    translate the case's own token-spelled ``expect.root`` for comparison.
    """
    tokens = {
        "<TMP>": str(tmp_path),
        "<SESSION_REPO>": str(tmp_path / "session-repo"),
        "<LIVE_TREE>": str(tmp_path / "live-tree"),
        "<KLABAUTER>": str(tmp_path / "klabauter"),
        "<OTHER_REPO>": str(tmp_path / "other-repo"),
    }

    settings_home = tmp_path / "settings-home"
    ml_dir = settings_home / "machine-local"
    ml_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(settings_home))
    monkeypatch.delenv("MACHINE_LOCAL_REGISTRY_DIR", raising=False)

    for token_dir in case.get("paths", {}).get("existing_dirs", []):
        real = Path(_substitute(token_dir, tokens))
        real.mkdir(parents=True, exist_ok=True)

    for fname, body in case.get("registry_files", {}).items():
        (ml_dir / fname).write_text(_substitute(body, tokens), encoding="utf-8")

    for var, val in case.get("env", {}).items():
        if val is None:
            monkeypatch.delenv(var, raising=False)
        else:
            monkeypatch.setenv(var, _substitute(val, tokens))

    session_repo = case["session_repo"]
    if session_repo in ("working", "other"):
        session_dir = Path(tokens["<SESSION_REPO>"])
        session_dir.mkdir(parents=True, exist_ok=True)
        (session_dir / ".git").mkdir(exist_ok=True)
        monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(session_dir))
        monkeypatch.chdir(session_dir)
    else:
        monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)
        cwd_dir = tmp_path / "cwd-no-git-ancestor"
        cwd_dir.mkdir(parents=True, exist_ok=True)
        monkeypatch.chdir(cwd_dir)

    return tokens


def _invoke(fn, error_type) -> Any:
    """Run *fn*; a raised *error_type* becomes a comparable sentinel tuple
    rather than propagating — DoE's own ladder never raises (fail-open,
    "Zero-spawn, fail-open, never raises", ``_engine_root.py``:469), so this
    driver treats the one documented fail-open error type as a comparable
    outcome. Only *error_type* is caught this way; any other exception is a
    bug, not a documented fail-open path, and propagates to hard-fail the
    case rather than being papered over as a comparable sentinel.
    # Review: tests-docs integration — docstring previously read as though
    # any raise were captured; only error_type is.
    """
    try:
        return fn()
    except error_type as exc:
        return ("raised", type(exc).__name__, str(exc))


if _DOE_ROOT is None:

    def test_engine_root_conformance_prerequisite_unregistered() -> None:
        pytest.skip(_SKIP_REASON)

else:
    _FIXTURE = _load_fixture(_DOE_ROOT)
    _CASES = _FIXTURE["cases"]
    _LIVE_CASE_IDS = {case["id"] for case in _CASES}

    # C5: the two xfail tables must be membership-disjoint — each names a
    # DISTINCT divergence cause (env-rung vs stamp-rule); a case appearing
    # in both would be ambiguous about which xfail reason actually applies.
    _XFAIL_TABLE_OVERLAP = set(_XFAIL_ENV_RUNG_REASON) & set(_XFAIL_STAMP_RULE_REASON)
    assert not _XFAIL_TABLE_OVERLAP, (
        f"case id(s) {sorted(_XFAIL_TABLE_OVERLAP)} appear in BOTH "
        "_XFAIL_ENV_RUNG_REASON and _XFAIL_STAMP_RULE_REASON — each table "
        "names a distinct divergence cause; a case must not carry two "
        "different xfail reasons at once"
    )

    # C5: close the tables' own silent-drift mode — a key naming a case
    # DoE later renames or removes is otherwise never looked up again (no
    # error, no warning, the marker silently inert). See both tables' own
    # docstrings/comments; this converts that into one named failure that
    # says exactly which case moved, rather than 50-70 concurrent sessions
    # going red unannounced when DoE's fixture changes shape.
    _STALE_XFAIL_KEYS = (
        set(_XFAIL_ENV_RUNG_REASON) | set(_XFAIL_STAMP_RULE_REASON)
    ) - _LIVE_CASE_IDS
    assert not _STALE_XFAIL_KEYS, (
        f"xfail table key(s) {sorted(_STALE_XFAIL_KEYS)} do not match any "
        "case id in the fixture loaded this run — DoE renamed or removed "
        "the case; update or remove the stale xfail table entry rather than "
        "leaving it silently inert"
    )

    def _xfail_marks(case_id: str) -> list:
        if case_id in _XFAIL_ENV_RUNG_REASON:
            return [pytest.mark.xfail(strict=True, reason=_XFAIL_ENV_RUNG_REASON[case_id])]
        if case_id in _XFAIL_STAMP_RULE_REASON:
            return [pytest.mark.xfail(strict=True, reason=_XFAIL_STAMP_RULE_REASON[case_id])]
        return []

    _params = [
        pytest.param(case, id=case["id"], marks=_xfail_marks(case["id"]))
        for case in _CASES
    ]

    @pytest.mark.parametrize("case", _params)
    def test_engine_root_conformance(
        case: dict[str, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        preconditions = case.get("preconditions") or {}
        if preconditions.get("sibling_engine_checkout_absent"):
            if not _sibling_engine_checkout_absent(_DOE_ROOT):
                pytest.skip(
                    f"{case['id']}: unmet precondition "
                    "sibling_engine_checkout_absent — this host has a "
                    "'claude-klabauter' sibling checkout next to the DoE clone, so "
                    "DoE's rung-3 __file__-relative sibling walk always answers "
                    "and the branch this case pins is unreachable declaratively "
                    "on this host."
                )

        tokens = _prepare_case(case, tmp_path, monkeypatch)
        engine_root_mod._reset_gate_memo()

        shim = engine_root_mod._load_shim()

        shim_actual = _invoke(
            shim.resolve_claude_klabauter_root_with_class, shim.ClaudeKlabauterResolutionError
        )
        wrapper_actual = _invoke(
            engine_root_mod.coordinator_engine_root_with_class,
            shim.ClaudeKlabauterResolutionError,
        )

        assert shim_actual == wrapper_actual, (
            f"{case['id']}: the shim and the coordinator_core wrapper — the two "
            "AC10 entrypoints over the single DR-132 implementation — disagree: "
            f"shim={shim_actual!r} wrapper={wrapper_actual!r}"
        )

        expect = case["expect"]
        expected_root = (
            _substitute(expect["root"], tokens) if expect["root"] is not None else None
        )
        expected = (expected_root, expect["resolution_class"])
        assert shim_actual == expected, (
            f"{case['id']}: expected {expected!r}, got {shim_actual!r}"
        )
