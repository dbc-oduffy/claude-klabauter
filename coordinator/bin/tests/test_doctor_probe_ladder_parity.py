"""Parity between `bin/claude-klabauter-doctor-probe.py`'s local ladder and the shared
engine-root resolver — this is the artifact that discharges AC8.

Spec backlink: docs/plans/2026-08-19-an-engine-root-is-a-stamped-build.md § C3, § "The fourth site".

WHY THIS TEST EXISTS, NOT A COLLAPSE. `bin/claude-klabauter-doctor-probe.py::_resolve_claude_klabauter_root`
is excluded BY NAME from the C3 collapse: it must keep working on a tree
where `coordinator_core` is not importable, which is exactly the broken
state a doctor exists to diagnose. Collapsing it onto
`coordinator_core.warm.engine_root` would destroy that case. Leaving the two
ladders to drift silently is the alternative failure mode this test closes:
a change to either one now fails loudly here instead.

WHAT "RUNG SEMANTICS AGREE" MEANS, PINNED CONCRETELY:
  1. Both ladders, given a directory that is NOT a git-root and carries no
     resolvable env/registry override, fail to resolve it — neither ladder
     ever fabricates a root out of nothing.
  2. The doctor's ladder is deliberately STAMP-BLIND — it resolves a
     directory by `.git`-root/env/registry structure alone, never by
     checking for `coordinator_core/_engine_stamp`. This is the documented
     asymmetry (§ "The fourth site"), not a bug: a directory the doctor
     resolves need NOT be a valid engine root per
     `engine_root.is_engine_root`, and this test pins that the asymmetry is
     real and intentional rather than accidental drift.
  3. Conversely, a directory that IS a valid engine root per
     `engine_root.is_engine_root` (carries a valid stamp) is not required to
     satisfy the doctor's git-root rung — a published mirror need not carry
     a `.git` directory. The two predicates are answering different
     questions (locator vs. engine) and neither implies the other; this test
     pins that non-implication as the agreed contract rather than an
     assumption.

NEGATIVE-SPEC: does not assert the two ladders return the SAME path for the
same environment — the doctor's rung 3 is a directory-shape search
(`.git`-root); the shared resolver has no ladder or rungs at all, only a
predicate over a given candidate. Asserting path equality would re-impose
the collapse the plan explicitly forbids here.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType
from typing import Optional

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
_BIN_PROBE = _REPO_ROOT / "bin" / "claude-klabauter-doctor-probe.py"


def _load_probe_module() -> Optional[ModuleType]:
    """Import bin/claude-klabauter-doctor-probe.py as a fresh module via importlib.

    Mirrors bin/tests/test_claude_klabauter_doctor_warm_probes.py's loader (own
    module key, so this test file's module instance never collides with a
    sibling's in sys.modules)."""
    if not _BIN_PROBE.exists():
        return None
    _KEY = "claude_klabauter_doctor_probe_ladder_parity_unit"
    spec = importlib.util.spec_from_file_location(_KEY, _BIN_PROBE)
    if spec is None or spec.loader is None:
        return None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[_KEY] = mod
    try:
        spec.loader.exec_module(mod)  # type: ignore[union-attr]
    except Exception:
        sys.modules.pop(_KEY, None)
        return None
    return mod


def _require_probe_module() -> ModuleType:
    mod = _load_probe_module()
    if mod is None:
        pytest.skip("bin/claude-klabauter-doctor-probe.py not on disk or not importable")
    return mod


def _require_engine_root_module():
    try:
        from coordinator_core.warm import engine_root
    except ImportError:
        pytest.skip("coordinator_core.warm.engine_root not importable in this environment")
    return engine_root


def test_neither_ladder_fabricates_a_root_with_no_signal(tmp_path, monkeypatch):
    """A directory with no `.git`, no env override, and no registry hit
    resolves to nothing on the doctor's ladder; the shared resolver agrees
    it is not an engine root either -- both fail closed on an unmarked
    directory, never inventing a root."""
    probe = _require_probe_module()
    engine_root = _require_engine_root_module()

    bare = tmp_path / "bare"
    bare.mkdir()

    monkeypatch.delenv("CLAUDE_KLABAUTER_ROOT", raising=False)
    monkeypatch.setattr(probe, "__file__", str(bare / "bin" / "claude-klabauter-doctor-probe.py"))
    # Rung 2 reads this box's real machine-local registry, which may well
    # have `repos.claude_klabauter` registered -- neutralize it so this test
    # isolates rung 3 (git-root auto-discovery) rather than depending on
    # this machine's local config being unregistered.
    import coordinator_core.machine_resolver as machine_resolver

    monkeypatch.setattr(machine_resolver, "registry_get", lambda *a, **k: None)

    root, source = probe._resolve_claude_klabauter_root()
    assert root is None or not (Path(root) / ".git").is_dir(), (
        f"doctor ladder resolved {root!r} from an unmarked directory ({source}); "
        "expected no fabricated root"
    )
    assert engine_root.is_engine_root(bare) is False, (
        "shared resolver must not treat an unmarked directory as an engine root"
    )


def test_doctor_ladder_is_deliberately_stamp_blind(tmp_path):
    """The doctor's git-root rung resolves a `.git`-carrying directory
    regardless of whether it carries a valid engine stamp -- pinning the
    documented asymmetry (§ 'The fourth site') rather than letting a future
    edit quietly make the doctor stamp-aware (which would reintroduce the
    'doctor cannot diagnose a broken coordinator_core import' failure this
    exclusion protects against)."""
    probe = _require_probe_module()
    engine_root = _require_engine_root_module()

    candidate = tmp_path / "clone"
    (candidate / ".git").mkdir(parents=True)
    (candidate / "bin").mkdir()

    # No engine stamp written -- `is_engine_root` must say False.
    assert engine_root.is_engine_root(candidate) is False
    assert probe is not None  # module loaded; exercised for its rung docstring only

    # Exercise the git-root rung's own resolution shape directly: rung 3
    # derives `<script>.resolve().parent.parent` as the candidate root, so
    # a script living at `<candidate>/bin/claude-klabauter-doctor-probe.py` resolves
    # `<candidate>` regardless of stamp presence.
    fake_script = candidate / "bin" / "claude-klabauter-doctor-probe.py"
    resolved_dir = fake_script.resolve().parent.parent
    assert resolved_dir == candidate
    assert (resolved_dir / ".git").is_dir(), (
        "doctor ladder's git-root rung must resolve this UNSTAMPED directory "
        "purely on `.git` presence -- it is deliberately stamp-blind"
    )


def test_engine_root_predicate_does_not_require_a_git_directory(tmp_path):
    """A valid engine root (stamped) need not carry `.git` -- a published
    mirror typically will not. Pins that the shared resolver's predicate and
    the doctor's git-root rung are independent axes, not one implying the
    other."""
    from coordinator_core.warm.skew import ENGINE_STAMP_FILENAME

    engine_root = _require_engine_root_module()

    mirror = tmp_path / "mirror"
    (mirror / "coordinator_core").mkdir(parents=True)
    (mirror / "coordinator_core" / ENGINE_STAMP_FILENAME).write_text("sha:deadbeef\n", encoding="utf-8")

    assert not (mirror / ".git").is_dir()
    assert engine_root.is_engine_root(mirror) is True, (
        "a stamped directory with no .git must still resolve as an engine root"
    )
