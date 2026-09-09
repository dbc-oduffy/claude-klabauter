"""`require_dispatch_engine_on_path` is a TRUE NO-OP for the inline preamble.

C16 collapses ~200 verbatim copies of a three-line bootstrap onto one seam. The
collapse is only safe if the seam is behaviourally indistinguishable from the
body it replaces — a collapse that also "improves" resolution is two changes
wearing one commit.

The near-miss that motivates this module: the obvious seam to adopt was
`require_engine_on_path`, which is the same shape on the LOCATOR axis. On a box
with both env vars unset the two ladders return different roots, so adopting it
would have repointed every converted CLI from the published engine to the working
tree. Three of 164 files changed exit code and that was the only visible symptom.
So the axis is pinned here by test, not by docstring.
"""

from __future__ import annotations

import importlib.util
import pathlib
import sys

import pytest

_LIB = pathlib.Path(__file__).resolve().parent.parent / "lib"


@pytest.fixture(scope="module")
def cc():
    spec = importlib.util.spec_from_file_location("cc_invoke_under_test", _LIB / "cc_invoke.py")
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _inline_preamble(resolve) -> str:
    """The exact body the ~200 CLIs carry, as the oracle to compare against."""
    claude_klabauter_root = resolve()
    if claude_klabauter_root not in sys.path:
        sys.path.insert(0, claude_klabauter_root)
    return claude_klabauter_root


def test_returns_the_dispatch_ladders_answer(cc, monkeypatch, tmp_path):
    """The seam resolves through `_resolve_claude_klabauter_root`, not the locator ladder."""
    sentinel = str(tmp_path / "published-engine")
    monkeypatch.setattr(cc, "_resolve_claude_klabauter_root", lambda: sentinel)
    monkeypatch.setattr(sys, "path", list(sys.path))

    assert cc.require_dispatch_engine_on_path() == sentinel


def test_matches_the_inline_body_it_replaces(cc, monkeypatch, tmp_path):
    """Same return value AND same sys.path mutation as the inline preamble."""
    sentinel = str(tmp_path / "published-engine")
    monkeypatch.setattr(cc, "_resolve_claude_klabauter_root", lambda: sentinel)

    monkeypatch.setattr(sys, "path", list(sys.path))
    inline_return = _inline_preamble(lambda: sentinel)
    inline_path = list(sys.path)

    monkeypatch.setattr(sys, "path", [p for p in sys.path if p != sentinel])
    seam_return = cc.require_dispatch_engine_on_path()
    seam_path = list(sys.path)

    assert seam_return == inline_return
    assert seam_path == inline_path


def test_inserts_at_the_front(cc, monkeypatch, tmp_path):
    """Front-insert, so an explicit override outranks an ambient editable install."""
    sentinel = str(tmp_path / "published-engine")
    monkeypatch.setattr(cc, "_resolve_claude_klabauter_root", lambda: sentinel)
    monkeypatch.setattr(sys, "path", ["/some/ambient/site-packages"])

    cc.require_dispatch_engine_on_path()
    assert sys.path[0] == sentinel


def test_is_idempotent(cc, monkeypatch, tmp_path):
    """A second call must not stack a duplicate entry — the inline body's `not in` check."""
    sentinel = str(tmp_path / "published-engine")
    monkeypatch.setattr(cc, "_resolve_claude_klabauter_root", lambda: sentinel)
    monkeypatch.setattr(sys, "path", list(sys.path))

    cc.require_dispatch_engine_on_path()
    cc.require_dispatch_engine_on_path()
    assert sys.path.count(sentinel) == 1


def test_propagates_runtime_error_like_the_inline_body(cc, monkeypatch):
    """Catches nothing: callers' own `except RuntimeError` remediation must still fire."""
    def _boom():
        raise RuntimeError("every rung missed")

    monkeypatch.setattr(cc, "_resolve_claude_klabauter_root", _boom)
    with pytest.raises(RuntimeError, match="every rung missed"):
        cc.require_dispatch_engine_on_path()


def test_takes_no_script_file_argument(cc):
    """The signature is the guard against silently drifting onto the locator axis.

    Every other `*_on_path` wrapper takes `script_file` and answers "where is the
    source checkout". This one answers "which engine executes", which is a property
    of the box. A signature that cannot accept a script path cannot be handed one.
    """
    import inspect

    params = inspect.signature(cc.require_dispatch_engine_on_path).parameters
    assert not params, f"expected a zero-argument seam, got {list(params)}"

    for locator_seam in ("require_engine_on_path", "require_colocated_engine_on_path"):
        assert "script_file" in inspect.signature(getattr(cc, locator_seam)).parameters, (
            f"{locator_seam} lost its script_file parameter — the axis distinction this "
            "test relies on has changed shape and the collapse needs re-checking"
        )


def test_require_dispatch_module_hot_path_never_touches_the_diff(cc, monkeypatch, tmp_path):
    """LOAD-BEARING: the failure-path diff (`_dotted_module_names_under`, two
    `rglob` walks -- DR-362's cost shape) must not run when the import
    succeeds. Asserts the hot path stayed clean, not that it merely returned
    fast: the diff helper is made to raise if called at all, so any success-
    path invocation of it fails the test regardless of timing.
    """
    sentinel = str(tmp_path / "published-engine")
    monkeypatch.setattr(cc, "_resolve_claude_klabauter_root", lambda: sentinel)
    monkeypatch.setattr(sys, "path", list(sys.path))

    def _must_not_be_called(root):
        raise AssertionError(f"diff helper ran on the success path (root={root!r})")

    monkeypatch.setattr(cc, "_dotted_module_names_under", _must_not_be_called)
    monkeypatch.setattr(
        cc.importlib, "import_module", lambda name: __import__("sys")
    )

    result = cc.require_dispatch_module("sys")
    assert result is not None


def test_require_dispatch_module_success_returns_the_imported_module(cc, monkeypatch, tmp_path):
    sentinel = str(tmp_path / "published-engine")
    monkeypatch.setattr(cc, "_resolve_claude_klabauter_root", lambda: sentinel)
    monkeypatch.setattr(sys, "path", list(sys.path))

    assert cc.require_dispatch_module("os") is __import__("os")


def test_require_dispatch_module_reports_stale_mirror_cause_and_remedy(cc, monkeypatch, tmp_path):
    """The published-engine-is-behind case: the missing module exists under
    the resolved SOURCE checkout's own `coordinator_core/` but not under the
    (fake) dispatch root -- the exact shape of the filed incident.
    """
    source_root = tmp_path / "source"
    dispatch_root = tmp_path / "dispatch"
    (source_root / "coordinator_core" / "session").mkdir(parents=True)
    (source_root / "coordinator_core" / "session" / "__init__.py").write_text("")
    (source_root / "coordinator_core" / "session" / "name_ladder.py").write_text("X = 1\n")
    (dispatch_root / "coordinator_core" / "session").mkdir(parents=True)
    (dispatch_root / "coordinator_core" / "session" / "__init__.py").write_text("")

    monkeypatch.setattr(cc, "require_dispatch_engine_on_path", lambda: str(dispatch_root))
    monkeypatch.setattr(cc, "resolve_engine_root", lambda script_file: str(source_root))

    def _boom(name):
        raise ModuleNotFoundError(
            "No module named 'coordinator_core.session.name_ladder'",
            name="coordinator_core.session.name_ladder",
        )

    monkeypatch.setattr(cc.importlib, "import_module", _boom)

    with pytest.raises(cc.StaleEngineImportError) as excinfo:
        cc.require_dispatch_module("coordinator_core.session.name_ladder")

    message = str(excinfo.value)
    assert "coordinator_core.session.name_ladder" in message
    assert "publish" in message.lower()
    assert excinfo.value.dotted_name == "coordinator_core.session.name_ladder"


def test_require_dispatch_module_reports_not_in_source_either(cc, monkeypatch, tmp_path):
    """The non-mirror-gap case: the missing name is absent from source too --
    a typo, not a publish lag. Must not tell the reader to publish.
    """
    source_root = tmp_path / "source"
    dispatch_root = tmp_path / "dispatch"
    (source_root / "coordinator_core").mkdir(parents=True)
    (dispatch_root / "coordinator_core").mkdir(parents=True)

    monkeypatch.setattr(cc, "require_dispatch_engine_on_path", lambda: str(dispatch_root))
    monkeypatch.setattr(cc, "resolve_engine_root", lambda script_file: str(source_root))

    def _boom(name):
        raise ModuleNotFoundError(
            "No module named 'coordinator_core.nonexistent_thing'",
            name="coordinator_core.nonexistent_thing",
        )

    monkeypatch.setattr(cc.importlib, "import_module", _boom)

    with pytest.raises(cc.StaleEngineImportError) as excinfo:
        cc.require_dispatch_module("coordinator_core.nonexistent_thing")

    message = str(excinfo.value)
    assert "not in source either" in message
    assert "publish" not in message.lower()


def test_require_dispatch_module_reuses_the_dispatch_seam_unchanged(cc, monkeypatch, tmp_path):
    """`require_dispatch_module` must not re-implement root resolution --
    it calls `require_dispatch_engine_on_path` itself, so that seam's own
    behaviour (divergence hardening, split announcement) is untouched.
    """
    sentinel = str(tmp_path / "published-engine")
    calls = []
    real = cc.require_dispatch_engine_on_path

    def _spy():
        calls.append(1)
        return real()

    monkeypatch.setattr(cc, "_resolve_claude_klabauter_root", lambda: sentinel)
    monkeypatch.setattr(sys, "path", list(sys.path))
    monkeypatch.setattr(cc, "require_dispatch_engine_on_path", _spy)
    monkeypatch.setattr(cc.importlib, "import_module", lambda name: __import__(name))

    cc.require_dispatch_module("os")
    assert calls == [1]


def test_the_two_axes_are_not_the_same_function(cc):
    """A refactor aliasing one to the other would silently undo the split."""
    assert cc.require_dispatch_engine_on_path is not cc.require_engine_on_path
    dispatch_src = cc.require_dispatch_engine_on_path.__code__.co_names
    assert "_resolve_claude_klabauter_root" in dispatch_src, (
        "the dispatch seam no longer calls the dispatch ladder"
    )
    assert "resolve_engine_root" not in dispatch_src, (
        "the dispatch seam now calls the LOCATOR ladder — this is the exact repointing "
        "the seam exists to prevent"
    )
