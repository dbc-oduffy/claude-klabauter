"""test_cc_invoke_provenance_reporting_seams.py — AC for C4, "Test the
wrapper and `_seam_present` reporting paths".

Chunk: docs/plans/2026-08-26-the-seam-reports-what-it-got.md § C4

C3 wired `_report_provenance` into every `*_on_path` wrapper
(`ensure_engine_on_path`, `require_engine_on_path`,
`require_colocated_engine_on_path`, `require_dispatch_engine_on_path`) and
into `_seam_present`, so each call site now REPORTS through
`provenance_against` with its own already-resolved `root` — never a
re-resolved or module-level one (AC12). This file tests that every one of
those five reporting call sites is actually wired (AC7), that a wrapper's
return value and `_front_insert_on_path`'s body are unchanged by the
reporting addition (AC8), that the unimported hot path short-circuits on
`sys.modules.get` before any filesystem call, checked structurally rather
than by wall clock (AC9 — a stopwatch flaps under this box's load norm), and
that each call site reports its OWN returned root, not a re-derived one
(AC12). It does NOT test where a divergence report is SENT — that is C6,
gated on the PM's sink choice (§ Sink decision).

Negative-spec: this file never asserts on wall-clock timing anywhere (AC9);
never asserts on where a `ProvenanceReport` is persisted or emitted (C6's
job, not this file's); never re-derives a root itself to compare against —
every assertion below compares a wrapper's OWN returned root against what it
reported, never a value this test file resolves independently.

Run: pytest coordinator/bin/tests/test_cc_invoke_provenance_reporting_seams.py -q
"""
from __future__ import annotations

import ast
import inspect
import sys
import textwrap
from pathlib import Path

import pytest

_TESTS_DIR = Path(__file__).resolve().parent
_BIN_DIR = _TESTS_DIR.parent
_LIB_DIR = _BIN_DIR / "lib"

if str(_LIB_DIR) not in sys.path:
    sys.path.insert(0, str(_LIB_DIR))

import cc_invoke as _mod  # noqa: E402  (import after path setup)

pytestmark = pytest.mark.cadence


@pytest.fixture
def clean_sys_path():
    """Restores `sys.path` to its pre-test contents afterward — the wrappers
    under test insert a resolved root onto `sys.path` (`_front_insert_on_path`),
    and several cases here feed them sentinel/fake roots that must not leak
    into sibling tests."""
    before = list(sys.path)
    try:
        yield
    finally:
        sys.path[:] = before


@pytest.fixture
def clean_sys_modules_coordinator_core():
    """Restores `sys.modules["coordinator_core"]` (present or absent) to its
    pre-test state afterward. `_seam_present`'s own docstring documents that
    a `find_spec` probe on a dotted name imports the parent package as a
    side effect and deliberately does NOT restore `sys.modules` — a case
    exercising that must not leak the resulting import into sibling tests."""
    sentinel = object()
    prior = sys.modules.get("coordinator_core", sentinel)
    try:
        yield
    finally:
        if prior is sentinel:
            sys.modules.pop("coordinator_core", None)
        else:
            sys.modules["coordinator_core"] = prior


def _record_calls(monkeypatch):
    """Monkeypatch `cc_invoke.provenance_against` to record every `root=`
    kwarg it is called with, returning a fixed non-raising result. Used for
    AC7/AC12 — this test file is not asserting on `provenance_against`'s own
    logic (C2's job), only on what each call site passes it."""
    calls = []

    def _fake(*, root):
        calls.append(root)
        return _mod.EngineProvenance(_mod.PROVENANCE_UNIMPORTED, None, None)

    monkeypatch.setattr(_mod, "provenance_against", _fake)
    return calls


# ---------------------------------------------------------------------------
# AC7 — every wrapper and `_seam_present` reports; none is silently unwired.
# AC12 — each call site reports its OWN returned root, never a re-resolved one.
# ---------------------------------------------------------------------------


def test_ensure_engine_on_path_reports_its_own_returned_root(monkeypatch, tmp_path, clean_sys_path):
    root = tmp_path / "engine-root-a"
    root.mkdir()
    monkeypatch.setattr(_mod, "resolve_engine_root", lambda script_file: str(root))
    calls = _record_calls(monkeypatch)

    returned = _mod.ensure_engine_on_path("irrelevant.py")

    assert returned == str(root)
    assert calls == [str(root)]


def test_require_engine_on_path_reports_its_own_returned_root(monkeypatch, tmp_path, clean_sys_path):
    root = tmp_path / "engine-root-b"
    root.mkdir()
    monkeypatch.setattr(_mod, "resolve_engine_root", lambda script_file: str(root))
    calls = _record_calls(monkeypatch)

    returned = _mod.require_engine_on_path("irrelevant.py")

    assert returned == str(root)
    assert calls == [str(root)]


def test_require_colocated_engine_on_path_reports_its_own_returned_root(
    monkeypatch, tmp_path, clean_sys_path
):
    root = tmp_path / "engine-root-c"
    root.mkdir()
    monkeypatch.setattr(_mod, "resolve_colocated_claude_klabauter_root", lambda script_file: str(root))
    calls = _record_calls(monkeypatch)

    returned = _mod.require_colocated_engine_on_path("irrelevant.py")

    assert returned == str(root)
    assert calls == [str(root)]


def test_require_dispatch_engine_on_path_reports_its_own_returned_root(
    monkeypatch, tmp_path, clean_sys_path
):
    root = tmp_path / "engine-root-d"
    root.mkdir()
    monkeypatch.setattr(_mod, "_resolve_claude_klabauter_root", lambda: str(root))
    calls = _record_calls(monkeypatch)

    returned = _mod.require_dispatch_engine_on_path()

    assert returned == str(root)
    assert calls == [str(root)]


def test_seam_present_reports_the_root_it_was_given(
    monkeypatch, tmp_path, clean_sys_path, clean_sys_modules_coordinator_core
):
    root = tmp_path / "engine-root-e"
    root.mkdir()
    calls = _record_calls(monkeypatch)

    _mod._seam_present(str(root))

    assert calls == [str(root)]


def test_require_dispatch_engine_on_path_traces_to_the_dispatch_resolver_not_locator(
    monkeypatch, tmp_path, clean_sys_path
):
    """AC12's named case: `require_dispatch_engine_on_path` must report the
    root from `_resolve_claude_klabauter_root` (dispatch axis), never from
    `resolve_engine_root` (locator axis) — the two ladders can return
    different roots on a conformant box (see `require_dispatch_engine_on_path`'s
    own docstring). Both resolvers are patched to distinct sentinel strings so
    a mix-up is unambiguous."""
    dispatch_sentinel = str(tmp_path / "dispatch-sentinel-root")
    locator_sentinel = str(tmp_path / "locator-sentinel-root")
    Path(dispatch_sentinel).mkdir()
    Path(locator_sentinel).mkdir()

    monkeypatch.setattr(_mod, "_resolve_claude_klabauter_root", lambda: dispatch_sentinel)
    monkeypatch.setattr(_mod, "resolve_engine_root", lambda script_file: locator_sentinel)
    calls = _record_calls(monkeypatch)

    returned = _mod.require_dispatch_engine_on_path()

    assert returned == dispatch_sentinel
    assert calls == [dispatch_sentinel]
    assert locator_sentinel not in calls


# ---------------------------------------------------------------------------
# AC8 — a wrapper's return value is still `root` unchanged, and
# `_front_insert_on_path`'s body was not modified by the reporting addition.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "wrapper_name,resolver_name,call_kwargs",
    [
        ("ensure_engine_on_path", "resolve_engine_root", {"script_file": "x.py"}),
        ("require_engine_on_path", "resolve_engine_root", {"script_file": "x.py"}),
        (
            "require_colocated_engine_on_path",
            "resolve_colocated_claude_klabauter_root",
            {"script_file": "x.py"},
        ),
        ("require_dispatch_engine_on_path", "_resolve_claude_klabauter_root", {}),
    ],
)
def test_wrapper_return_value_is_still_root_unchanged(
    monkeypatch, tmp_path, clean_sys_path, wrapper_name, resolver_name, call_kwargs
):
    root = tmp_path / f"root-for-{wrapper_name}"
    root.mkdir()
    if resolver_name in ("resolve_engine_root", "resolve_colocated_claude_klabauter_root"):
        monkeypatch.setattr(_mod, resolver_name, lambda script_file, _r=str(root): _r)
    else:
        monkeypatch.setattr(_mod, resolver_name, lambda _r=str(root): _r)
    _record_calls(monkeypatch)

    wrapper = getattr(_mod, wrapper_name)
    returned = wrapper(**call_kwargs)

    assert returned == str(root), (
        f"{wrapper_name} must still return its resolved root unchanged after "
        "the reporting addition"
    )


def test_front_insert_on_path_body_was_not_modified():
    source = textwrap.dedent(inspect.getsource(_mod._front_insert_on_path))
    tree = ast.parse(source)
    func_node = tree.body[0]
    assert isinstance(func_node, ast.FunctionDef)

    # Docstring (optional) + If + Return is the whole body — reporting must
    # not have been folded into this shared insert primitive.
    body = [n for n in func_node.body if not (isinstance(n, ast.Expr) and isinstance(n.value, ast.Constant))]
    assert len(body) == 2, (
        "_front_insert_on_path's body must still be exactly an If followed "
        "by a Return — reporting belongs at the wrapper call sites, not "
        "inside this shared primitive"
    )
    assert isinstance(body[0], ast.If)
    assert isinstance(body[1], ast.Return)
    assert isinstance(body[1].value, ast.Name) and body[1].value.id == "root"

    for node in ast.walk(func_node):
        if isinstance(node, ast.Call):
            func = node.func
            called_name = func.attr if isinstance(func, ast.Attribute) else (
                func.id if isinstance(func, ast.Name) else None
            )
            assert called_name != "provenance_against", (
                "_front_insert_on_path must not itself call provenance_against "
                "— reporting is a wrapper-call-site concern, never this shared "
                "insert primitive's"
            )


# ---------------------------------------------------------------------------
# AC9 — the unimported hot path short-circuits on `sys.modules.get` before
# any filesystem call, asserted structurally (no `Path.resolve`/`os.path`
# call reached), never by wall clock.
# ---------------------------------------------------------------------------


def test_unimported_hot_path_makes_no_filesystem_call_via_the_wrapper_reporting_seam(
    monkeypatch, tmp_path, clean_sys_path, clean_sys_modules_coordinator_core
):
    # Review: code-reviewer P1 (slice f80de67e1) — an `AssertionError` raised
    # from inside a monkeypatched `Path.resolve` would be swallowed by
    # `_report_provenance`'s own outer `except Exception` (a broad catch
    # required by hard constraint 3), so a trap that RAISES from inside that
    # function's body can never surface a regression. Count calls instead —
    # a return value the `except Exception` cannot intercept.
    sys.modules.pop("coordinator_core", None)

    calls = []
    real_resolve = Path.resolve

    def _counting_resolve(self, *args, **kwargs):
        calls.append(self)
        return real_resolve(self, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", _counting_resolve)
    root = tmp_path / "root-unimported"
    monkeypatch.setattr(_mod, "resolve_engine_root", lambda script_file: str(root))

    returned = _mod.ensure_engine_on_path("irrelevant.py")

    assert str(returned) == str(root)
    assert calls == [], (
        "provenance_against must short-circuit on sys.modules.get before "
        f"ever touching the filesystem via Path.resolve() — got {calls!r}"
    )


# ---------------------------------------------------------------------------
# Hard constraint 2 AT WRAPPER SCOPE — asking must never import the engine.
#
# AC2 exercises `provenance_against` directly, and it passes even when a
# wrapper imports `coordinator_core` on the way to the sink: the sink lives
# under `coordinator_core`, and `_report_provenance`'s outer `except
# Exception` swallows anything a probe raises from inside it, so neither AC2
# nor AC9's `Path.resolve` trap can see the import. This is the seam's own
# failure mode turned on itself — the detector binding the package it exists
# to observe, and every later call in the process then measuring a binding
# the detector caused. Asserted here on `sys.modules` directly, which no
# `except` can hide.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "wrapper_name",
    [
        "ensure_engine_on_path",
        "require_engine_on_path",
        "require_colocated_engine_on_path",
    ],
)
def test_wrapper_does_not_import_the_engine_when_it_was_not_already_imported(
    wrapper_name, monkeypatch, tmp_path, clean_sys_path, clean_sys_modules_coordinator_core
):
    sys.modules.pop("coordinator_core", None)
    root = tmp_path / f"unimported-{wrapper_name}"
    root.mkdir()
    monkeypatch.setattr(_mod, "resolve_engine_root", lambda script_file: str(root))
    monkeypatch.setattr(_mod, "resolve_colocated_claude_klabauter_root", lambda script_file: str(root))

    getattr(_mod, wrapper_name)("irrelevant.py")

    assert "coordinator_core" not in sys.modules, (
        f"{wrapper_name} imported coordinator_core as a side effect of asking "
        "where coordinator_core came from (hard constraint 2). The sink lives "
        "under coordinator_core, so the report call must return on an "
        "`unimported` verdict BEFORE importing it."
    )


def test_seam_present_reporting_binds_no_engine_beyond_its_own_probe(
    monkeypatch, tmp_path, clean_sys_path, clean_sys_modules_coordinator_core
):
    sys.modules.pop("coordinator_core", None)
    root = tmp_path / "seam-present-unimported"
    root.mkdir()

    # `_seam_present`'s `find_spec` probe is documented to leave
    # `coordinator_core` in `sys.modules` when it resolves one — that is the
    # sanctioned asymmetry, not this test's subject. Subject: a root the probe
    # CANNOT resolve must leave the engine unbound, so the reporting call
    # added beside it is not quietly binding what the probe declined to.
    monkeypatch.setattr(
        _mod.importlib.util, "find_spec", lambda name: None
    )

    _mod._seam_present(str(root))

    assert "coordinator_core" not in sys.modules, (
        "_seam_present's reporting call imported coordinator_core even though "
        "its own probe resolved nothing (hard constraint 2)"
    )


# ---------------------------------------------------------------------------
# Constraint 3 (Finding 9) — a report-site call raising must not propagate
# out of a wrapper or `_seam_present`; each still returns `root` unchanged.
# ---------------------------------------------------------------------------


def _raising_provenance_against(*, root):
    raise RuntimeError("simulated report-site failure — must not propagate")


@pytest.mark.parametrize(
    "wrapper_name,resolver_name,call_kwargs",
    [
        ("ensure_engine_on_path", "resolve_engine_root", {"script_file": "x.py"}),
        ("require_engine_on_path", "resolve_engine_root", {"script_file": "x.py"}),
        (
            "require_colocated_engine_on_path",
            "resolve_colocated_claude_klabauter_root",
            {"script_file": "x.py"},
        ),
        ("require_dispatch_engine_on_path", "_resolve_claude_klabauter_root", {}),
    ],
)
def test_wrapper_survives_a_raising_provenance_against(
    monkeypatch, tmp_path, clean_sys_path, wrapper_name, resolver_name, call_kwargs
):
    root = tmp_path / f"survives-{wrapper_name}"
    root.mkdir()
    if resolver_name in ("resolve_engine_root", "resolve_colocated_claude_klabauter_root"):
        monkeypatch.setattr(_mod, resolver_name, lambda script_file, _r=str(root): _r)
    else:
        monkeypatch.setattr(_mod, resolver_name, lambda _r=str(root): _r)
    monkeypatch.setattr(_mod, "provenance_against", _raising_provenance_against)

    wrapper = getattr(_mod, wrapper_name)
    returned = wrapper(**call_kwargs)

    assert returned == str(root), (
        f"{wrapper_name} must still return its resolved root unchanged even "
        "when the reporting call raises"
    )


def test_seam_present_survives_a_raising_provenance_against(
    monkeypatch, tmp_path, clean_sys_path, clean_sys_modules_coordinator_core
):
    root = tmp_path / "seam-present-survives"
    root.mkdir()

    # Hard constraint 6: the probe's own answer is NOT asserted against a
    # literal. `find_spec("coordinator_core.invoke")` can reach an ambient
    # `coordinator_core` (an editable install, or a repo root pytest put on
    # `sys.path[0]`) regardless of the empty root injected here, so a
    # hard-coded `False` pins this box's install layout rather than the
    # property under test. The property IS invariance: whatever the probe
    # answers, a raising reporting call must not change it and must not
    # propagate. Baseline first, with reporting intact, then re-probe.
    baseline = _mod._seam_present(str(root))

    monkeypatch.setattr(_mod, "provenance_against", _raising_provenance_against)
    result = _mod._seam_present(str(root))

    assert result == baseline, (
        "_seam_present must still return its own find_spec-derived answer "
        "unchanged even when the reporting call raises"
    )


# ---------------------------------------------------------------------------
# Constraint 4 (Finding 9) — the reporting code (`_report_provenance`, and
# the report call at each wrapper/`_seam_present` call site) contains no
# `sys.path` mutation of its own. Greppable/AST-checkable, mirroring AC11's
# raise-ban shape in the sibling C2 test file.
# ---------------------------------------------------------------------------


def _assert_no_sys_path_mutation(func):
    source = textwrap.dedent(inspect.getsource(func))
    tree = ast.parse(source)
    func_node = tree.body[0]
    assert isinstance(func_node, ast.FunctionDef)
    for node in ast.walk(func_node):
        if isinstance(node, ast.Attribute) and node.attr == "path":
            value = node.value
            if isinstance(value, ast.Name) and value.id == "sys":
                pytest.fail(
                    f"{func.__name__} must not reference sys.path at all — "
                    "reporting code must never mutate sys.path"
                )


def test_report_provenance_has_no_sys_path_mutation():
    _assert_no_sys_path_mutation(_mod._report_provenance)


def test_seam_present_report_call_site_adds_no_new_sys_path_mutation():
    """`_seam_present` legitimately mutates `sys.path` for its find_spec probe
    (inject/remove around the probe) — that is pre-existing and out of this
    guard's scope. This asserts the reporting CALL itself
    (`_report_provenance("_seam_present", claude_klabauter_root, "dispatch")`) sits
    AFTER the probe's own `finally` cleanup, i.e. is not nested inside the
    inject/remove `try`/`finally` block, so reporting cannot re-mutate
    `sys.path` on the caller's behalf."""
    source = textwrap.dedent(inspect.getsource(_mod._seam_present))
    tree = ast.parse(source)
    func_node = tree.body[0]
    assert isinstance(func_node, ast.FunctionDef)

    try_nodes = [n for n in ast.walk(func_node) if isinstance(n, ast.Try)]
    assert try_nodes, "_seam_present must still wrap its find_spec probe in a try/finally"
    for try_node in try_nodes:
        for sub in ast.walk(try_node):
            if isinstance(sub, ast.Call):
                func = sub.func
                called_name = func.attr if isinstance(func, ast.Attribute) else (
                    func.id if isinstance(func, ast.Name) else None
                )
                assert called_name != "_report_provenance", (
                    "_report_provenance must not be called from inside "
                    "_seam_present's find_spec try/finally block"
                )


# ---------------------------------------------------------------------------
# Anti-scope mechanism cases (Finding 12).
# ---------------------------------------------------------------------------


def test_a_sys_modules_state_left_by_seam_present_is_not_restored(
    monkeypatch, tmp_path, clean_sys_path, clean_sys_modules_coordinator_core
):
    """(a) `_seam_present`'s `find_spec` probe imports the parent package
    `coordinator_core` into `sys.modules` as a documented side effect and
    deliberately does NOT restore it — only `sys.path` is restored. This
    pins the sanctioned-path asymmetry: "fixing" it by popping
    `sys.modules["coordinator_core"]` after the probe would fail this test."""
    fake_root = tmp_path / "fake-engine-root"
    pkg_dir = fake_root / "coordinator_core"
    pkg_dir.mkdir(parents=True)
    (pkg_dir / "__init__.py").write_text("", encoding="utf-8")
    (pkg_dir / "invoke.py").write_text("", encoding="utf-8")

    sys.modules.pop("coordinator_core", None)
    assert "coordinator_core" not in sys.modules

    result = _mod._seam_present(str(fake_root))

    assert result is True
    assert "coordinator_core" in sys.modules, (
        "find_spec's parent-package import side effect must survive "
        "_seam_present's own return — sys.modules is deliberately not "
        "restored, only sys.path is (see _seam_present's own docstring)"
    )


# ---------------------------------------------------------------------------
# Review: code-reviewer P1 — `_report_provenance` must pass a real `cwd` to
# the sink, or `resolve_git_root_cheap(None)`'s `if not cwd: return None`
# guard fires unconditionally and the counter silently never writes. A test
# that only mocks the sink and asserts it was called would NOT catch this —
# the defect is that the REAL sink no-ops. This exercises the real sink
# against a real git checkout on disk and asserts a record actually landed.
# ---------------------------------------------------------------------------


def test_report_provenance_actually_writes_a_record_through_the_real_sink(
    monkeypatch, tmp_path, clean_sys_path
):
    import json

    fake_repo = tmp_path / "fake-repo-for-real-sink"
    fake_repo.mkdir()
    # resolve_git_root_cheap only does os.path.exists(cwd/".git") — a plain
    # marker file reproduces the same walk without spawning real git.
    (fake_repo / ".git").mkdir()

    monkeypatch.chdir(fake_repo)
    monkeypatch.setattr(
        _mod,
        "provenance_against",
        lambda *, root: _mod.EngineProvenance(_mod.PROVENANCE_MATCH, "/x/y.py", root),
    )

    report = _mod._report_provenance("ensure_engine_on_path", str(fake_repo), "locator")

    assert report.verdict == _mod.PROVENANCE_MATCH

    counts_file = fake_repo / "state" / "engine-provenance-counts.jsonl"
    assert counts_file.is_file(), (
        "record_engine_provenance must have written a real record to "
        "state/engine-provenance-counts.jsonl under the resolved git root — "
        "if this file does not exist, _report_provenance failed to pass a "
        "resolvable cwd to the sink and the write silently no-opped"
    )
    lines = counts_file.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["caller"] == "ensure_engine_on_path"
    assert record["axis"] == "locator"
    assert record["verdict"] == _mod.PROVENANCE_MATCH


def test_publish_time_rename_transform_call_site_still_exists():
    """(b) the `_resolve_claude_klabauter_root` → `_resolve_claude_klabauter_root`
    publish-time rename transform's call site (the dual-export alias line)
    still exists — the mirror transform relies on finding this exact
    assignment to rewrite on the way out to a published tree."""
    source = inspect.getsource(_mod)
    assert "_resolve_claude_klabauter_root = _resolve_claude_klabauter_root" in source, (
        "the publish-time rename transform's dual-export alias "
        "(_resolve_claude_klabauter_root = _resolve_claude_klabauter_root) must still "
        "exist verbatim as a call/assignment site in cc_invoke.py"
    )
