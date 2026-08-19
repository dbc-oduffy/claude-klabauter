"""coordinator_core.ceremony_common.test_phantom_resolves_id_sweep — the
fleet-wide generalization (2026-07-27) of the phantom-resolves-id guard
that previously covered `workstream_complete` ONLY
(`test_workstream_complete.test_no_judgment_point_resolves_a_phantom_
directive_id`).

RULE. Every `coordinator_core` package that defines a `brief(` entrypoint
AND ever emits a judgment_point disposition naming a non-empty `resolves`
is either (a) covered by a registered sweep provider in `_phantom_sweep_
providers.py`, (b) verified — dynamically or, where a dynamic call would
need disk/git fixtures disproportionate to the risk, via a static source
scan — to never emit a non-empty `resolves` at all, or (c) named in
`_DEFERRED_ALLOWLIST` below with a concrete, non-appetite reason. There is
no fourth option: an unregistered, unverified, unallowlisted package with
judgment points fails this suite by name (`test_every_discovered_package_
is_registered_or_allowlisted`) rather than silently passing because no
sweep ever ran against it — the exact failure mode the prior workstream_
complete-only guard could not catch for any OTHER package.

Spec backlink: cross-repo/inbox/2026-07-27-… "Generalize seam guards
fleet-wide" dispatch (DoE-claude, 2026-07-27). Prior single-package
instance: `coordinator_core/workstream_complete/test_workstream_complete.py`.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from coordinator_core.ceremony_common import _phantom_sweep_providers as providers
from coordinator_core.ceremony_common.phantom_resolves_sweep import (
    PhantomSweepResult,
    assert_no_phantom_resolves_ids,
    discover_brief_defining_packages,
)

_COORDINATOR_CORE_ROOT = Path(__file__).resolve().parents[1]

#: dynamic-suffix bases (per-package) -- resolves ids only ever emitted
#: with a caller-computed suffix.
#:
#: EMPTY BY CONSTRUCTION, and it must stay that way. This exemption was not a
#: benign notation: `apply`'s gate matches a `resolves` entry against a
#: directive id EXACTLY, never by prefix, so a `resolves` naming an unsuffixed
#: BASE names no directive at all. Exempting the base from the phantom sweep
#: therefore did not describe a safe pattern -- it silenced the one guard that
#: would have caught a permanently-shut gate.
#:
#: The four ids once listed here (`d-add-lesson`, `d-queue-append-lesson`,
#: `d-flip-memo-status`, `d-freeze-and-dispatch-review-partition`) were ALL
#: live defects, reported by example-retrieval-repo-em 2026-07-28 after a captured lesson
#: silently failed to reach disk while `apply` still reported success. Each
#: judgment point now receives the exact suffixed ids from the same builder
#: that emits the directives (`*_resolves_ids()` helpers), so there is nothing
#: left to exempt.
#:
#: Negative-spec: a new entry here is almost certainly a bug being suppressed.
#: The correct fix for "my resolves ids are computed at runtime" is to derive
#: them from the directive builder itself, never to exempt the base.
_DYNAMIC_SUFFIX_BASES: dict[str, frozenset[str]] = {}

#: no-directive-backing resolves ids (per-package) -- a `resolves` id
#: naming a step with NO backing `directives[]` entry at all, by design.
_NO_DIRECTIVE_BACKING_IDS: dict[str, frozenset[str]] = {
    "workstream_complete": frozenset({"d-render-final-summary"}),
}

#: Registered sweep providers -- each wrapped to the uniform
#: `(monkeypatch, tmp_path) -> PhantomSweepResult` call shape the
#: parametrized test below uses, even though most providers need neither
#: fixture (they call pure builder functions with hand-built input).
_PROVIDERS: dict[str, object] = {
    "workday_complete": lambda monkeypatch, tmp_path: providers.sweep_workday_complete(),
    "workweek_complete": lambda monkeypatch, tmp_path: providers.sweep_workweek_complete(),
    "workstream_complete": lambda monkeypatch, tmp_path: providers.sweep_workstream_complete(monkeypatch, tmp_path),
    "baton_assemble": lambda monkeypatch, tmp_path: providers.sweep_baton_assemble(),
    "merge_assemble": lambda monkeypatch, tmp_path: providers.sweep_merge_assemble(),
    "consolidate_assemble": lambda monkeypatch, tmp_path: providers.sweep_consolidate_assemble(tmp_path),
    "backlog_grind_assemble": lambda monkeypatch, tmp_path: providers.sweep_backlog_grind_assemble(monkeypatch),
    "review_assemble": lambda monkeypatch, tmp_path: providers.sweep_review_assemble(monkeypatch, tmp_path),
    "pickup_assemble": lambda monkeypatch, tmp_path: providers.sweep_pickup_assemble(monkeypatch, tmp_path),
}

#: Packages verified (dynamically, below) to emit `judgment_points` whose
#: dispositions NEVER carry a non-empty `resolves` -- there is no phantom-
#: id risk to sweep, but the claim is checked every run, not just asserted
#: in a comment. `orient_assemble` and `learn_lessons_assemble` are cheap
#: to call directly (read-only, no complex fixture); each entry maps to
#: the callable that performs the live verification.
def _verify_orient_assemble_never_resolves() -> None:
    from coordinator_core import orient_assemble as oa

    for cadence in oa.CADENCES:
        do = oa.brief(cadence)
        for jp in do["judgment_points"]:
            for disposition in jp["dispositions"]:
                assert not disposition.get("resolves"), (
                    f"orient_assemble: judgment point {jp['id']!r} disposition "
                    f"{disposition['value']!r} now resolves {disposition['resolves']!r} -- "
                    "this package was verified resolves-free; register a real sweep "
                    "provider in _phantom_sweep_providers.py instead of relying on this "
                    "static verification."
                )


def _verify_learn_lessons_assemble_never_resolves(tmp_path: Path) -> None:
    from coordinator_core import learn_lessons_assemble as lla

    result = lla.brief("does/not/exist.md", "incoming text", repo_root=tmp_path)
    assert result.decision_object["judgment_points"] == [], (
        "learn_lessons_assemble: expected judgment_points=[] always (module never emits "
        "one, per its own brief() docstring) -- if this now emits a judgment point, it "
        "needs a real sweep provider registered in _phantom_sweep_providers.py, not this "
        "verified-empty allowlist entry."
    )


def _assert_package_source_never_resolves(package_name: str) -> None:
    """Static source scan for a `resolves` key anywhere in *package_name*.

    The RULE this module states allows a static scan where a dynamic call
    would need disk/git fixtures disproportionate to the risk, and both
    callers are exactly that case: `plan_assemble.residue.brief()` loads
    DoE's `coordinator/skills/plan/residue` segment directory, which the
    autouse `_quarantine_real_home` fixture stubs away, and
    `quick_wrap_assemble.brief()` raises without a git worktree AND a
    resolvable session id. Calling either under a stub would return early
    without inspecting a single disposition — a check that passes because
    it did nothing, which is worse than no check.

    Scanning source instead is strictly stronger here: it fails on a
    `resolves` added down ANY branch, including one this repo's fixtures
    could not reach. It is a coarser signal (a `resolves` in a docstring or
    a comment trips it), and that is the correct direction to be wrong in —
    the remedy is to register a real sweep provider, which is what the
    package would then need anyway."""
    package_root = _COORDINATOR_CORE_ROOT / package_name
    assert package_root.is_dir(), f"{package_name}: package directory not found"
    offenders = []
    for source in sorted(package_root.rglob("*.py")):
        if source.name.startswith("test_") or "tests" in source.parts:
            continue
        text = source.read_text(encoding="utf-8")
        if '"resolves"' in text or "'resolves'" in text:
            offenders.append(str(source.relative_to(_COORDINATOR_CORE_ROOT)))
    assert not offenders, (
        f"{package_name}: a `resolves` key now appears in {offenders} — this package "
        "was registered as verified resolves-free, so its judgment points were never "
        "swept for phantom directive ids. Register a real sweep provider in "
        "_phantom_sweep_providers.py rather than widening this scan."
    )


_VERIFIED_RESOLVES_FREE = frozenset(
    {
        "orient_assemble",
        "learn_lessons_assemble",
        # Both landed after the 2026-07-27 fleet-wide generalization and were
        # never registered, so `test_every_discovered_package_is_registered_
        # or_allowlisted` had been failing by name — working exactly as
        # designed. Verified resolves-free rather than allowlisted: neither
        # emits a `resolves` anywhere in its source.
        "plan_assemble",
        "quick_wrap_assemble",
    }
)

#: Packages that emit `brief(` but are deliberately NOT swept here, with a
#: concrete, non-appetite reason each -- never a silent skip.
#:
#: Empty as of 2026-07-27 (follow-up dispatch): `pickup_assemble` was the
#: sole entry (5764-line module, bespoke inline `resolves` dict-literals
#: across its memo/handoff/spinoff kind-dispatch) and is now covered by
#: `_PROVIDERS["pickup_assemble"]` -> `providers.sweep_pickup_assemble`
#: instead. Left as an explicit empty dict, not deleted, so a future
#: deferral has an obvious place to land and this history stays visible.
_DEFERRED_ALLOWLIST: dict[str, str] = {}


def _discovered_packages() -> dict[str, Path]:
    return discover_brief_defining_packages(_COORDINATOR_CORE_ROOT)


def test_every_discovered_package_is_registered_or_allowlisted() -> None:
    """Fail by name -- never silently skip -- for any `brief(`-defining
    package that is neither a registered sweep provider, a verified-
    resolves-free package, nor a named `_DEFERRED_ALLOWLIST` entry. A
    newly-added assembler package with judgment points must show up in
    exactly one of these three buckets, or this test names it."""
    discovered = _discovered_packages()
    known = set(_PROVIDERS) | _VERIFIED_RESOLVES_FREE | set(_DEFERRED_ALLOWLIST)
    unregistered = set(discovered) - known
    assert not unregistered, (
        f"{sorted(unregistered)} define brief( but are registered in NONE of: "
        "_PROVIDERS (a real sweep), _VERIFIED_RESOLVES_FREE (checked to never resolve "
        "anything), or _DEFERRED_ALLOWLIST (a named, reasoned deferral) in "
        "test_phantom_resolves_id_sweep.py -- add one of the three rather than let this "
        "package's judgment points go unswept silently."
    )


@pytest.mark.parametrize("package_name", sorted(_PROVIDERS))
def test_no_phantom_resolves_id(package_name: str, monkeypatch, tmp_path) -> None:
    provider = _PROVIDERS[package_name]
    result: PhantomSweepResult = provider(monkeypatch, tmp_path)
    assert_no_phantom_resolves_ids(
        result,
        package_name=package_name,
        dynamic_suffix_bases=_DYNAMIC_SUFFIX_BASES.get(package_name, frozenset()),
        no_directive_backing_ids=_NO_DIRECTIVE_BACKING_IDS.get(package_name, frozenset()),
    )


def test_orient_assemble_verified_resolves_free() -> None:
    _verify_orient_assemble_never_resolves()


def test_learn_lessons_assemble_verified_resolves_free(tmp_path) -> None:
    _verify_learn_lessons_assemble_never_resolves(tmp_path)


@pytest.mark.parametrize("package_name", ["plan_assemble", "quick_wrap_assemble"])
def test_source_verified_resolves_free(package_name: str) -> None:
    _assert_package_source_never_resolves(package_name)


def test_pickup_assemble_sweep_covers_every_classification_and_memo_kind(monkeypatch, tmp_path) -> None:
    """Named-regression test (2026-07-27 follow-up dispatch, per its own
    instruction: sweep coverage of `pickup_assemble` "all three [...] AND,
    within memo, all four `kind` values" must fail loud, not silently pass,
    if a future edit narrows `pickup_assemble_variants` back down to
    partial coverage. Checks CLASSIFICATION coverage against each variant's
    own emitted `artifact.classification` (never trusts the variant NAME
    alone to mean what it says) and `kind` coverage against the variant
    names this module documents as one-per-`_KIND_DISPOSITIONS`-key (kind
    itself is not threaded back out onto the decision object, so the name
    is the only observable handle for that axis)."""
    variants = providers.pickup_assemble_variants(monkeypatch, tmp_path)

    classifications = {name: result.decision_object["artifact"]["classification"] for name, result in variants}
    seen_classifications = set(classifications.values())
    assert seen_classifications == {"handoff", "spinoff", "memo"}, (
        f"pickup_assemble sweep only reached classifications {sorted(seen_classifications)} -- "
        "expected all three of handoff/spinoff/memo; a variant was dropped or its "
        "classification drifted"
    )

    variant_names = {name for name, _ in variants}
    expected_kind_variants = {f"memo-kind-{kind}" for kind in ("ask", "consult", "proposal", "fyi")}
    missing_kind_variants = expected_kind_variants - variant_names
    assert not missing_kind_variants, (
        f"pickup_assemble sweep is missing memo-kind variant(s) {sorted(missing_kind_variants)} -- "
        "expected one swept variant per _KIND_DISPOSITIONS key (ask/consult/proposal/fyi)"
    )

    assert "handoff-live-claim-bail" in variant_names, (
        "pickup_assemble sweep no longer reaches the handoff/spinoff live-claim stand-down "
        "bail (__init__.py ~5121-5176) -- the exact variant this deferral's flagged finding "
        "required covering"
    )
