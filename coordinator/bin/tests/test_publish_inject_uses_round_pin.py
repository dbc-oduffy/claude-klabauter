"""test_publish_inject_uses_round_pin — the inject path materializes from the
ROUND-PINNED sha, not a fresh `ref="HEAD"` read.

REGRESSION NET for a measured defect. `docs/plans/2026-08-04-publish-from-a-
committed-ref.md` C1b added `_round_pin_source_sha` so that "a publish round is
a snapshot of one commit" survives a box where peers commit mid-round. That pin
was threaded through `process_target` and `run_pre_sync_gates` — but NOT
through `_materialize_inject_srcs`, whose C4/AC6 work landed separately and
called `_git_materialize_ref(git_root)` with the default ref.

The consequence was not merely a wasted ~40s full-tree extraction per round.
The injected CI-harness payload shipped from a LATER commit than the sha the
round pinned, printed as `Provenance: <root> shipped from <sha>`, and promised
to doe-claude-em as reproducible (C6). One publish, two commits, one provenance
line. Observed 2026-08-19: three distinct SHAs materialized for a single git
toplevel inside one round.

These tests are STUB-ONLY and spawn no git — the sibling suites covering this
area (`test_publish_round_pin_and_identity_attribution.py`,
`coordinator/tests/test_publish_materialize_inject_srcs.py`) both carry
`spawns_process`/`cadence`, so a real-git version of this check would only run
at a cadence gate. The threading is what regresses, and it is assertable
without a repo.

Run: python -m pytest coordinator/bin/tests/test_publish_inject_uses_round_pin.py -q
"""

from __future__ import annotations

import importlib.util
import inspect
import sys
from pathlib import Path

_BIN_DIR = Path(__file__).resolve().parent.parent


def _load_publish_module():
    spec = importlib.util.spec_from_file_location(
        "publish_inject_round_pin_under_test", _BIN_DIR / "publish.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


publish = _load_publish_module()


def _section(src: Path) -> dict:
    return {"inject": [{"src": str(src), "dest": "somewhere"}]}


def test_inject_materializes_at_the_round_pinned_sha(monkeypatch, tmp_path):
    """The pin already in the round's dict is what inject materializes at —
    `_git_materialize_ref` must receive it as `ref`, never fall back to HEAD."""
    src_dir = tmp_path / "content-root"
    src_dir.mkdir()
    materialize_calls: "list[tuple[Path, str]]" = []

    def _fake_pin(root, pinned_shas, *, out=None, late=False):
        return pinned_shas.setdefault(str(root), "PINNED_SHA")

    def _fake_materialize(root, ref="HEAD"):
        materialize_calls.append((root, ref))
        return src_dir

    monkeypatch.setattr(publish, "_round_pin_source_sha", _fake_pin)
    monkeypatch.setattr(publish, "_git_materialize_ref", _fake_materialize)

    pins: "dict[str, str]" = {}
    publish._materialize_inject_srcs(_section(src_dir), tmp_path, pins)

    assert materialize_calls, "inject never materialized its src at all"
    assert all(
        ref == "PINNED_SHA" for _root, ref in materialize_calls
    ), f"inject materialized at a non-pinned ref: {materialize_calls!r}"


def test_inject_reuses_a_pin_the_round_already_resolved(monkeypatch, tmp_path):
    """A sha pinned earlier in the round (by main's round-start pass or an
    earlier row) is reused verbatim — inject must not resolve its own."""
    src_dir = tmp_path / "content-root"
    src_dir.mkdir()
    seen_refs: "list[str]" = []

    monkeypatch.setattr(
        publish,
        "_round_pin_source_sha",
        lambda root, pinned_shas, *, out=None, late=False: pinned_shas[str(root)],
    )
    monkeypatch.setattr(
        publish,
        "_git_materialize_ref",
        lambda root, ref="HEAD": (seen_refs.append(ref), src_dir)[1],
    )

    pins = {str(src_dir): "ROUND_START_SHA"}
    publish._materialize_inject_srcs(_section(src_dir), tmp_path, pins)

    assert seen_refs == ["ROUND_START_SHA"], (
        "inject did not reuse the round's existing pin — a second sha in one "
        f"round is the defect this guards: {seen_refs!r}"
    )


def test_no_unpinned_materialize_call_survives_in_the_inject_path():
    """Source guard: the bare `_git_materialize_ref(git_root)` form is what
    regressed, and it reads as harmless. Name it explicitly."""
    source = inspect.getsource(publish._materialize_inject_srcs)
    assert "_git_materialize_ref(git_root)" not in source, (
        "inject is materializing at the default ref='HEAD' again — it must "
        "pass the round-pinned sha (see this module's docstring)."
    )
    assert "ref=sha" in source, "expected the pinned-ref materialize call"


def test_pin_is_threaded_from_process_target_to_inject():
    """The pin only helps if it actually arrives. Both hops are guarded, since
    either one silently reverts inject to its own HEAD read."""
    dispatch_src = inspect.getsource(publish.dispatch_percolate_inject)
    assert "round_pinned_shas" in inspect.signature(
        publish.dispatch_percolate_inject
    ).parameters, "dispatch_percolate_inject must accept the round pin"
    assert "_materialize_inject_srcs(section, percolate_root, round_pinned_shas)" in dispatch_src, (
        "dispatch_percolate_inject must forward the round pin to "
        "_materialize_inject_srcs"
    )

    process_target_src = inspect.getsource(publish.process_target)
    assert "round_pinned_shas=round_pinned_shas" in process_target_src, (
        "process_target must hand its round pin to dispatch_percolate_inject"
    )


def test_absent_pin_still_works_for_direct_callers(monkeypatch, tmp_path):
    """A caller passing no pin (unit tests driving dispatch directly) keeps
    today's behaviour rather than raising on a missing dict."""
    src_dir = tmp_path / "content-root"
    src_dir.mkdir()

    monkeypatch.setattr(
        publish,
        "_round_pin_source_sha",
        lambda root, pinned_shas, *, out=None, late=False: pinned_shas.setdefault(
            str(root), "CALL_SCOPED_SHA"
        ),
    )
    monkeypatch.setattr(publish, "_git_materialize_ref", lambda root, ref="HEAD": src_dir)

    result = publish._materialize_inject_srcs(_section(src_dir), tmp_path)

    assert result["inject"][0]["src"] == str(src_dir)
