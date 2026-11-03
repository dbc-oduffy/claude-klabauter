"""call_signature cost: zero spawns, a bounded process-time delta over leak_gate without it.

Measured with time.process_time(): median of 5 samples, each the per-run mean of 8 leak_gate runs
(process_time ticks at 15.6ms on Windows; batching resolves a sub-tick cost). base = the wired
detectors without call_signature, after = WIRED_DETECTORS.
  (a) tmp_path, a landing adding calls into many dirty callees whose edits leave every signature
      alone: the detector parses nothing; delta stays under MAX_ADDED_MS.
  (b) tmp_path, one dirty callee with a widened signature that the landing calls: the finding.
  (c) terminal_commit.py of the live repo with one keyword line added to a multi-line call: zero
      spawns, added process time under MAX_ADDED_MS and after under PROCESS_TIME_TARGET_MS. The
      ceiling is never raised.

Callee discovery is bounded by the commit's own lines (imports whose names the added lines and their
enclosing head lines mention), one listing per directory, and one read per kept callee compared to
the HEAD blob (CRLF-folded). The unbounded form, every import of the landing, measured 703-1015ms
on shape (c) (about 2300 per-candidate stats, 95 of 108 CRLF-checked-out callees read as dirty).

Reference numbers (process ms, median of 5, spawns base/after):
  (a) body-only dirty callees: base 0.00, after 1.95-3.91, delta 1.95-3.91, spawns 0/0
  (b) dirty-callee finding:    base 0.00, after 0.00-1.95, delta 0.00-1.95, spawns 0/0
  (c) terminal_commit edit:    base 5.86-7.81, after 11.72-13.67, delta 5.86, spawns 0/0
"""

from __future__ import annotations

import statistics
import subprocess
import time
from pathlib import Path

import pytest

from coordinator_core import authoring_leaks
from coordinator_core.authoring_leaks import WIRED_DETECTORS, call_signature, leak_gate
from coordinator_core.authoring_leaks.tests._git_fixture import git as _git, write as _write
from coordinator_core.benchmarks.probe_commit_gates import PROCESS_TIME_TARGET_MS

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

RUNS = 5
BATCH = 8
MAX_ADDED_MS = 15.0
REPO_ROOT = Path(__file__).resolve().parents[3]
TERMINAL_COMMIT = "coordinator_core/ops/dispatch_emit/terminal_commit.py"


def _detector_sets():
    after, failures = authoring_leaks._resolve_default_detectors(WIRED_DETECTORS)
    assert not failures
    assert call_signature in after
    return [d for d in after if d is not call_signature], after


def _median_ms(root: Path, paths: list, detectors: list, monkeypatch, landed_text=None):
    """Median per-run process ms over `RUNS` samples of `BATCH` leak_gate runs each, plus the Popen count over all of them."""
    spawns: list = []
    real_popen = subprocess.Popen

    class _Counting(real_popen):
        def __init__(self, *a, **kw):
            spawns.append(a[:1])
            super().__init__(*a, **kw)

    monkeypatch.setattr(subprocess, "Popen", _Counting)
    leak_gate(root, paths, detectors=detectors, landed_text=landed_text)
    samples = []
    outcome = None
    for _ in range(RUNS):
        t0 = time.process_time()
        for _ in range(BATCH):
            outcome = leak_gate(root, paths, detectors=detectors, landed_text=landed_text)
        samples.append((time.process_time() - t0) * 1000.0 / BATCH)
    monkeypatch.setattr(subprocess, "Popen", real_popen)
    return statistics.median(samples), len(spawns), outcome


def test_body_only_dirty_callees_add_little_and_spawn_nothing(tmp_path, monkeypatch):
    _git(tmp_path, "init", "-q")
    _write(tmp_path, "pkg/__init__.py", "")
    imports, calls = [], []
    for i in range(40):
        _write(tmp_path, f"pkg/m{i}.py", f"def f{i}(a, b=None):\n    return a\n")
        imports.append(f"from pkg.m{i} import f{i}")
        calls.append(f"    f{i}(1, b=2)")
    head_landing = "\n".join(imports) + "\n\n\ndef run():\n    pass\n"
    _write(tmp_path, "pkg/landing.py", head_landing)
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "base")
    for i in range(40):
        _write(tmp_path, f"pkg/m{i}.py", f"def f{i}(a, b=None):\n    return a + {i}\n")
    _write(tmp_path, "pkg/landing.py", "\n".join(imports) + "\n\n\ndef run():\n" + "\n".join(calls) + "\n")

    base, after = _detector_sets()
    base_ms, base_spawns, _ = _median_ms(tmp_path, ["pkg/landing.py"], base, monkeypatch)
    parses: list = []
    real_parse = call_signature.ast.parse
    monkeypatch.setattr(call_signature.ast, "parse", lambda *a, **k: parses.append(a[:1]) or real_parse(*a, **k))
    after_ms, after_spawns, after_out = _median_ms(tmp_path, ["pkg/landing.py"], after, monkeypatch)
    monkeypatch.setattr(call_signature.ast, "parse", real_parse)
    delta = after_ms - base_ms
    assert parses == [], "a body-only edit must not parse the landing or a callee"
    print(f"shape (a): base={base_ms:.2f}ms after={after_ms:.2f}ms delta={delta:.2f}ms spawns={base_spawns}/{after_spawns}")

    assert after_out.passed
    assert base_spawns == 0 and after_spawns == 0
    assert delta <= MAX_ADDED_MS
    assert after_ms <= PROCESS_TIME_TARGET_MS


def test_terminal_commit_live_repo_spawns_nothing_and_stays_under_the_brightline(monkeypatch):
    original = (REPO_ROOT / TERMINAL_COMMIT).read_text(encoding="utf-8")
    anchor = "    integrity_extra = _landing_integrity_gate(\n"
    assert anchor in original
    landed = original.replace(anchor, anchor + "        withheld=None,\n", 1)
    assert landed != original
    base, after = _detector_sets()
    base_ms, base_spawns, _ = _median_ms(REPO_ROOT, [TERMINAL_COMMIT], base, monkeypatch, lambda rel: landed)
    after_ms, after_spawns, _ = _median_ms(REPO_ROOT, [TERMINAL_COMMIT], after, monkeypatch, lambda rel: landed)
    print(f"shape (c): base={base_ms:.2f}ms after={after_ms:.2f}ms delta={after_ms - base_ms:.2f}ms spawns={base_spawns}/{after_spawns}")

    assert base_spawns == 0 and after_spawns == 0
    assert after_ms - base_ms <= MAX_ADDED_MS
    assert after_ms <= PROCESS_TIME_TARGET_MS


def test_dirty_callee_shape_spawns_nothing(tmp_path, monkeypatch):
    _git(tmp_path, "init", "-q")
    _write(tmp_path, "pkg/__init__.py", "")
    _write(tmp_path, "pkg/b.py", "def f(a):\n    return a\n")
    _write(tmp_path, "pkg/a.py", "from pkg.b import f\n\n\ndef g():\n    return f(1)\n")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "base")
    _write(tmp_path, "pkg/b.py", "def f(a, withheld=None):\n    return a\n")
    _write(tmp_path, "pkg/a.py", "from pkg.b import f\n\n\ndef g():\n    return f(1, withheld=2)\n")

    base, after = _detector_sets()
    base_ms, base_spawns, base_out = _median_ms(tmp_path, ["pkg/a.py"], base, monkeypatch)
    after_ms, after_spawns, after_out = _median_ms(tmp_path, ["pkg/a.py"], after, monkeypatch)
    print(f"shape (b): base={base_ms:.2f}ms after={after_ms:.2f}ms delta={after_ms - base_ms:.2f}ms spawns={base_spawns}/{after_spawns}")

    assert base_out.passed and not after_out.passed
    assert "pkg/b.py" in after_out.diagnostics[0]
    assert base_spawns == 0 and after_spawns == 0
