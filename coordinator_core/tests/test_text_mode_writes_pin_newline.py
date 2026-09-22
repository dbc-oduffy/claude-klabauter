"""Ratchet: production text-mode file writes must pin `newline=`.

Python's text mode translates ``"\\n"`` to ``"\\r\\n"`` when the interpreter runs
on Windows, unless the caller passes ``newline="\\n"`` or ``newline=""``. Every
coordinator write without it therefore emits CRLF on a Windows host, no matter
what the target repo's ``.gitattributes`` declares.

Nothing signals it. ``core.autocrlf=true`` normalizes CRLF back out on the way
into the index, so the index stays clean, ``git status`` and ``git diff`` show
nothing, and the damage is working-tree-only and Windows-only. It ran across the
fleet for months before example-cockpit-repo-em caught it from the consumer side
(memo 2026-08-19); a macOS-only maintainer of these scripts could not observe it
even in principle. What it breaks is not cosmetic: a guard that parses source
line-by-line gets a trailing ``\\r`` and an end-of-line-anchored pattern quietly
stops matching, which is worse than no guard.

Negative-spec, three parts.

1. SOURCE-SHAPE, not behavioural, deliberately. A behavioural test would have to
   write a real file, and Python only translates newlines on Windows -- so the
   behavioural version passes vacuously on macOS and on Linux CI, i.e. exactly
   the hosts where the defect is invisible. AST parsing is the only check that is
   equally red everywhere.
2. NO SUBPROCESS. File discovery walks the filesystem rather than shelling
   ``git ls-files``: a git spawn would put this test under the spawn ratchet
   (``test_no_new_spawning_tests.py``) and force it off the fast tier, where a
   ratchet has to live to be worth anything.
3. TEST CODE IS OUT OF SCOPE, and that is a scoping decision, not an oversight.
   Tests write to ``tmp_path``, where no EOL policy exists; sweeping them was
   explicitly not asked for. Production code is what reaches a working tree.

``newline=""`` is accepted alongside ``newline="\\n"``: both disable translation,
and ``""`` is what the ``csv`` module requires. Binary mode is skipped -- it never
translates.

Spec backlink: state/sizings/2026-08-19-lf-convention-for-text-mode-writes-that.yaml
"""

from __future__ import annotations

import ast
import pathlib

REPO = pathlib.Path(__file__).resolve().parents[2]

#: Directories that never hold production writes we own.
_SKIP_DIRS = frozenset({
    ".git", "__pycache__", "node_modules", ".venv", "venv",
    ".pytest_cache", ".mypy_cache", "_vendor", "archive",
})

_OPEN_NAMES = frozenset({"open"})
#: ``os.fdopen``, ``io.open``, ``Path.open`` all take the same mode/newline pair.
_OPEN_ATTRS = frozenset({"fdopen", "open"})
_TEXT_WRITE_ATTRS = frozenset({"write_text"})


def _is_test_file(rel: str) -> bool:
    parts = rel.split("/")
    base = parts[-1]
    return (
        base.startswith("test_")
        or base.endswith("_test.py")
        or base == "conftest.py"
        or "tests" in parts
    )


def _literal_mode(call: ast.Call) -> str | None:
    """The call's mode as a literal, or None when absent/non-literal."""
    for kw in call.keywords:
        if kw.arg == "mode":
            v = kw.value
            return v.value if isinstance(v, ast.Constant) and isinstance(v.value, str) else None
        if kw.arg is None:  # **kwargs -- mode may arrive dynamically
            return None
    if len(call.args) >= 2:
        v = call.args[1]
        return v.value if isinstance(v, ast.Constant) and isinstance(v.value, str) else None
    return None


def _writes_text(call: ast.Call, attr: str) -> bool:
    if attr in _TEXT_WRITE_ATTRS:
        return True
    mode = _literal_mode(call)
    if mode is None:
        return False  # default "r", or dynamic -- not ours to assert on
    if "b" in mode:
        return False
    return any(c in mode for c in "wax+")


def _pins_newline(call: ast.Call) -> bool:
    for kw in call.keywords:
        if kw.arg == "newline":
            return True
        if kw.arg is None:  # **kwargs could carry it; do not accuse
            return True
    return False


class TextWriteParseError(AssertionError):
    """A scanned file did not parse, so this guard has no idea what it
    contains. Named rather than bare so the failure reads as "the guard
    could not look", never as "the guard looked and found nothing"."""


def _offenders_in(src: str, rel: str) -> list[str]:
    try:
        tree = ast.parse(src)
    except SyntaxError as exc:
        # RAISE, DO NOT RETURN []. This swallowed the error until 2026-09-06,
        # which meant an unparseable file was reported as COMPLIANT -- the
        # one verdict the guard is least entitled to give about a file it
        # could not read. Observed, not theorised: a genuinely broken
        # `coordinator/bin/retire-dead-post-commit-hooks.py` passed this
        # guard in the same run that `spawn_policy.detect` failed on it,
        # because that scanner raises `SpawnParseError` here instead. A
        # corpus guard's silence has to mean "I checked", or it means
        # nothing at all.
        raise TextWriteParseError(
            f"{rel}: failed to parse -- {exc}. This guard cannot certify a "
            f"file it cannot read; fix the syntax error rather than the guard."
        ) from exc
    out = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Name) and func.id in _OPEN_NAMES:
            attr = "open"
        elif isinstance(func, ast.Attribute) and (
            func.attr in _OPEN_ATTRS or func.attr in _TEXT_WRITE_ATTRS
        ):
            attr = func.attr
        else:
            continue
        if not _writes_text(node, attr):
            continue
        if _pins_newline(node):
            continue
        out.append(f"{rel}:{node.lineno}  {attr}(...)")
    return out


def _production_sources() -> list[tuple[str, str]]:
    found = []
    for path in REPO.rglob("*.py"):
        if _SKIP_DIRS.intersection(path.parts):
            continue
        rel = path.relative_to(REPO).as_posix()
        if _is_test_file(rel):
            continue
        try:
            found.append((rel, path.read_text(encoding="utf-8")))
        except (OSError, UnicodeDecodeError):
            continue
    return found


def test_no_production_text_write_omits_newline() -> None:
    sources = _production_sources()
    assert sources, "discovery found no production sources — the walk is broken"

    offenders = []
    for rel, src in sources:
        offenders.extend(_offenders_in(src, rel))

    assert not offenders, (
        f"{len(offenders)} production text-mode write(s) omit newline=. On a Windows "
        'host these emit CRLF into whatever tree they write, and core.autocrlf=true '
        "hides it from git on both sides — so nothing will tell you.\n"
        'Fix: add newline="\\n" (or newline="" for csv). Sites:\n  '
        + "\n  ".join(sorted(offenders))
    )


def test_an_unparseable_file_is_a_failure_not_a_pass() -> None:
    """The guard must not certify what it could not read. Proving the
    instrument: without this, `except SyntaxError: return []` reads exactly
    like a clean file to every caller, and the corpus scan below silently
    shrinks by one."""
    import pytest

    with pytest.raises(TextWriteParseError) as caught:
        _offenders_in("def f(:" + chr(10), "broken.py")
    assert "broken.py" in str(caught.value)
    assert "cannot certify" in str(caught.value)


def test_guard_detects_a_planted_violation() -> None:
    """The ratchet is worthless if it cannot go red — prove it does.

    Mirrors the planted-violation convention already used by
    test_async_handler_discipline_planted_violation.py.
    """
    planted = 'import pathlib\np = pathlib.Path("x")\np.write_text("hi", encoding="utf-8")\n'
    assert _offenders_in(planted, "planted.py"), "guard failed to flag a bare write_text"

    for clean in (
        'open("f", "w", newline="\\n")',
        'open("f", "w", newline="")',
        'open("f", "wb")',
        'open("f")',
        'open("f", "r", encoding="utf-8")',
    ):
        assert not _offenders_in(clean, "clean.py"), f"guard false-positived on: {clean}"


#: Production sources across the tree that previously wrote text mode
#: without pinning `newline=` -- spanning bare `open`/`os.fdopen` context
#: managers and `Path.write_text`, with and without an `encoding=` kwarg
#: already present. Each now pins `newline="\n"` at its write site.
_PINNED_PRODUCTION_SITES = (
    "coordinator/bin/check-settings-env.py",
    "coordinator/bin/classify-legacy-engine-noun-references.py",
    "coordinator/bin/compose-review-wave.py",
    "coordinator/bin/corpus-currency-probe.py",
    "coordinator/bin/emit-guard-enforcement-join.py",
    "coordinator/bin/generate-claudemeta-manifest.py",
    "coordinator/bin/land-wave.py",
    "coordinator/bin/mise-prep-upgrade.py",
    "coordinator/bin/tier-last-run.py",
    "coordinator/bin/waste-signal.py",
    "coordinator/lib/percolate/publish_sync.py",
    "coordinator/templates/bin/_machine_local.py",
    "coordinator_core/frontmatter/author_dependence.py",
    "coordinator_core/group_em/atomic_record.py",
    "coordinator_core/hooks/group_em_park_spool.py",
    "coordinator_core/hooks/guard_doctrine_surface_ratio_precommit.py",
    "coordinator_core/hooks/nudge_multiwave_workflow.py",
    "coordinator_core/hooks/pickup_autofire.py",
    "coordinator_core/hooks/runtime_tripwire_stop_watcher.py",
    "coordinator_core/hooks/session_start_announce_job_mode.py",
    "coordinator_core/hooks/session_start_register_doe_claude_root.py",
    "coordinator_core/hooks/session_start_repair_prepare_commit_msg_hook.py",
    "coordinator_core/hooks/session_start_write_plugin_root_breadcrumb.py",
    "coordinator_core/hooks/support/bin_impl_drift.py",
    "coordinator_core/hooks/support/next_move_ledger.py",
    "coordinator_core/ops/dispatch_emit/op.py",
    "coordinator_core/ops/docindex_emit.py",
    "coordinator_core/ops/fleet/memo_send.py",
    "coordinator_core/warm/http_hook_forwarder.py",
    "coordinator_core/warm/server.py",
    "coordinator_core/workflow_watch/stamp.py",
    "scripts/cloud_setup.py",
    "setup/dist/publish-repo-setup/dev-sync.py",
    "setup/dist/publish-repo-setup/install.py",
    "setup/dist/publish-repo-setup/name-personas.py",
)


def test_named_production_sites_pin_newline() -> None:
    """Each site in `_PINNED_PRODUCTION_SITES` must scan clean on its own --
    isolating them from the rest of the corpus walk means this test still
    catches a regression at these exact sites even while an unrelated
    scratch tree elsewhere under the repo root carries its own, separately
    tracked offenders."""
    for rel in _PINNED_PRODUCTION_SITES:
        path = REPO / rel
        src = path.read_text(encoding="utf-8")
        offenders = _offenders_in(src, rel)
        assert not offenders, f"{rel} still has unpinned text-mode write(s): {offenders}"


def test_named_production_sites_would_have_failed_unpinned() -> None:
    """Proves the assertion above is not vacuous: the exact write shapes
    these sites used before being pinned are ones `_offenders_in` flags."""
    unpinned_shapes = (
        'path.write_text(json.dumps(doc, indent=2) + "\\n", encoding="utf-8")',
        'with open(args.output, "w", encoding="utf-8") as handle:\n    pass',
        'with os.fdopen(fd, "w", encoding="utf-8") as f:\n    pass',
        'tmp.write_text(json.dumps(settings, indent=2))',
        'orphaned_at_path.write_text(orphaned_at)',
    )
    for shape in unpinned_shapes:
        assert _offenders_in(shape, "before.py"), f"guard failed to flag: {shape}"
