"""Per-gate process-time profile of the four klabauter publish end-of-run gates, plus a drift probe.

Calls `dispatch_end_of_run_repo_key_literal_gate`, `dispatch_end_of_run_live_identifier_gate`,
`dispatch_end_of_run_function_gate` and `dispatch_end_of_run_argv_parity_gate` of `coordinator/bin/publish.py`
in process, the way `main` calls them, over the real `claude-klabauter*` rows (source_dir and resolved
percolate section) and a gate root standing in for the assembled destination tree. Process time is this
process plus descendants (a job object on Windows, `RUSAGE_CHILDREN` elsewhere); wall clock is never recorded.

Legs per gate:

  cold   the first call in this process, over a work dir emptied at start (`--keep-work-dir` skips the wipe)
  warm   the second call and, while it stays under `REPEAT_CEILING_MS`, `--warm-repeat - 1` more, averaged
  drift  one repo-key literal and one env-var literal, both read by literal in the real source, renamed by an
         injected `substitute` rule; both literal gates must return False

A gate that returns False on an unmodified tree makes its timings inadmissible (the work stopped early).
Round-log timings (`[timing]` lines) are `publish_phase_cpu.parse_log`'s; this module adds the in-process leg.

    python coordinator_core/benchmarks/publish_end_of_run_gates_profile.py
    python coordinator_core/benchmarks/publish_end_of_run_gates_profile.py --gates argv_parity --json
"""

from __future__ import annotations

import argparse
import contextlib
import copy
import importlib.util
import io
import json
import os
import re
import shutil
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Optional

_REPO = Path(__file__).resolve().parents[2]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from coordinator_core.benchmarks import budget as _budget  # noqa: E402
from coordinator_core.benchmarks import declare_benchmark_origin  # noqa: E402

BAR_MS = 500.0
REPEAT_CEILING_MS = 1000.0
ROW_PREFIX = "claude-klabauter"
EXIT_OK = 0
EXIT_FAILED = 1
EXIT_SETUP = 2

GATES: tuple[str, ...] = ("repo_key_literal", "live_identifier", "function", "argv_parity")
LITERAL_GATES: tuple[str, ...] = ("repo_key_literal", "live_identifier")
GATE_LABELS: dict[str, str] = {g: f"dispatch_end_of_run_{g}_gate" for g in GATES}
BUDGET_KEYS: dict[str, str] = {g: f"publish.end_of_run_{g}_gate" for g in GATES}
_ENV_NAME = re.compile(r"[A-Z][A-Z0-9_]{3,}")
_WORK_DIR = _REPO / "scratch" / "publish-end-of-run-gates-profile"
_MAX_CANDIDATES = 12


class SetupError(RuntimeError):
    """The profile could not assemble a real round (no engine, no store, no klabauter row)."""


class _Meter:
    """Process-time clock over this process plus descendants; `instrument` names the source."""

    def __init__(self) -> None:
        self._win = None
        self.instrument = "process_time (this process only)"
        if sys.platform == "win32":
            try:
                from coordinator_core.benchmarks.process_time import LiveTreeAccountant

                self._win = LiveTreeAccountant(os.getpid())
                self.instrument = "job object (this process plus descendants)"
            except Exception:  # noqa: BLE001 - nested-job refusal falls back to own-process time
                self._win = None
        else:
            self.instrument = "process_time + RUSAGE_CHILDREN"

    def now_ms(self) -> float:
        if self._win is not None:
            return float(self._win.snapshot()["process_time_ms"])
        own = time.process_time() * 1000.0
        try:
            import resource

            kids = resource.getrusage(resource.RUSAGE_CHILDREN)
            own += (kids.ru_utime + kids.ru_stime) * 1000.0
        except ImportError:
            pass
        return own


@dataclass
class Leg:
    process_ms: float
    ok: bool
    n: int = 1


@dataclass
class GateProfile:
    gate: str
    cold: Optional[Leg] = None
    warm: Optional[Leg] = None

    @property
    def warm_within_bar(self) -> bool:
        return self.warm is not None and self.warm.ok and self.warm.process_ms <= BAR_MS


@dataclass
class DriftProbe:
    repo_key: str = ""
    env_var: str = ""
    caught: dict = field(default_factory=dict)
    process_ms: dict = field(default_factory=dict)
    detail: str = ""

    @property
    def passed(self) -> bool:
        return bool(self.caught) and all(self.caught.get(g) for g in LITERAL_GATES)


def load_publish_module():
    """`coordinator/bin/publish.py` loaded by path, the way its own tests load it."""
    path = _REPO / "coordinator" / "bin" / "publish.py"
    spec = importlib.util.spec_from_file_location("publish_end_of_run_gates_profile_publish", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    from coordinator_core.bin_lib_binding import exec_module_bin_bound

    exec_module_bin_bound(spec.loader, module, str(path.parent))
    return module


def build_round(publish: Any, row_prefix: str = ROW_PREFIX) -> tuple[Any, list]:
    """`(engine_ctx, [(ResolvedTarget, section)])` for every existing-source row named `row_prefix*`."""
    publish._bootstrap_engine()
    setup_dir = publish._resolve_percolate_root_and_rung(override=None)[0] / "setup"
    try:
        engine = publish._import_claude_klabauter_percolate()
        store = publish.assert_percolate_store_ready(engine, publish.locate_percolate_store(setup_dir))
    except Exception as exc:  # noqa: BLE001
        raise SetupError(f"percolate engine or store unavailable: {exc}") from exc
    rows = publish._load_rows(setup_dir, target_filter="", err=io.StringIO())
    sections = []
    for row in rows:
        target = publish.parse_target_row(row)
        if target.name.startswith(row_prefix) and target.source_dir.is_dir():
            sections.append((target, engine.resolve_target(store, target.name)))
    if not sections:
        raise SetupError(f"no {row_prefix}* row with an existing source_dir under {setup_dir}")
    ctx = publish.PercolateEngineContext(engine_claude_klabauter=engine, store=store)
    return ctx, sections


def gate_calls(
    publish: Any,
    engine_ctx: Any,
    sections: list,
    gate_root: Path,
    git_dir: Path,
    *,
    parse_only: bool = False,
) -> dict[str, Callable[[], bool]]:
    """One zero-argument callable per gate, each running the publish.py gate function as `main` does."""
    return {
        "repo_key_literal": lambda: publish.dispatch_end_of_run_repo_key_literal_gate(engine_ctx, sections),
        "live_identifier": lambda: publish.dispatch_end_of_run_live_identifier_gate(engine_ctx, sections),
        "function": lambda: publish.dispatch_end_of_run_function_gate(
            engine_ctx,
            [gate_root],
            target_filtered=False,
            parse_only=parse_only,
            git_dirs={gate_root: git_dir},
        ),
        "argv_parity": lambda: publish.dispatch_end_of_run_argv_parity_gate([gate_root], target_filtered=False),
    }


def _quiet(fn: Callable[[], bool]) -> tuple[bool, str]:
    err = io.StringIO()
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
        ok = bool(fn())
    return ok, err.getvalue()


def _timed(meter: _Meter, fn: Callable[[], bool], n: int = 1) -> Leg:
    t0 = meter.now_ms()
    ok = True
    for _ in range(n):
        ok = _quiet(fn)[0] and ok
    return Leg((meter.now_ms() - t0) / n, ok, n)


def profile_gate(meter: _Meter, name: str, call: Callable[[], bool], warm_repeat: int) -> GateProfile:
    """Cold leg, then the warm leg. Repeats shrink tick quantisation but stop once a call is over the ceiling."""
    prof = GateProfile(name, cold=_timed(meter, call))
    first = _timed(meter, call)
    if first.process_ms < REPEAT_CEILING_MS and warm_repeat > 1:
        rest = _timed(meter, call, warm_repeat - 1)
        total = first.process_ms + rest.process_ms * rest.n
        prof.warm = Leg(total / warm_repeat, first.ok and rest.ok, warm_repeat)
    else:
        prof.warm = first
    return prof


def drift_value(literal: str) -> str:
    """A replacement that does not contain `literal`: the live-identifier gate counts substring survivors."""
    return "drift~" + literal[::-1]


def inject_rules(section: Mapping[str, Any], rules: list[tuple[str, str]]) -> dict:
    """Copy of `section` with plain `substitute` rules prepended and the hook active."""
    out = copy.deepcopy(dict(section))
    out["substitute"] = [{"key": k, "value": v, "match_mode": "plain"} for k, v in rules] + list(
        out.get("substitute") or []
    )
    hooks = list(out.get("hooks") or [])
    if "substitute" not in hooks:
        hooks.append("substitute")
    out["hooks"] = hooks
    return out


def _source_files(source_dir: Path):
    """Non-test `.py` files in the order and with the exclusions the literal gates walk."""
    for path in sorted(source_dir.rglob("*.py")):
        rel = path.relative_to(source_dir).as_posix()
        if "tests" in Path(rel).parts[:-1] or path.name.startswith("test_") or "_vendor" in Path(rel).parts:
            continue
        try:
            yield rel, path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue


def _candidates(source_dir: Path, rkl: Any, live: Any):
    """`(kind, literal, rel, text)` for each repo-key literal and env-var name the source reads by literal."""
    import ast

    for rel, text in _source_files(source_dir):
        if "repos." in text or "REPO_" in text:
            for literal in rkl.repo_key_literals(text):
                if literal not in rkl.INTENDED_RENAMES:
                    yield "repo_key", literal, rel, text
        if "environ" in text or "getenv" in text:
            try:
                tree = ast.parse(text)
            except SyntaxError:
                continue
            for ident in sorted(live.live_identifiers(tree)):
                if _ENV_NAME.fullmatch(ident):
                    yield "env_var", ident, rel, text


def _transform_for(pct_engine: Any, section: Mapping[str, Any]) -> Callable[[str, str], str]:
    params = pct_engine._resolve_transform_params(section)  # noqa: SLF001 - the row's real transform
    return lambda text, rel: pct_engine._apply_content_transforms(text, params, rel)  # noqa: SLF001


def find_drift_rules(sections: list, pct_engine: Any, rkl: Any, live: Any) -> tuple[Any, Any, dict] | None:
    """`(target, drift_section, {kind: literal})` for the first row where a repo-key literal and an env-var
    name, each unprotected by that row, are renamed by an injected rule and the gate's own per-file check
    sees the rename. `None` when no row has both."""
    for target, section in sections:
        picked: dict = {}
        tried = {"repo_key": 0, "env_var": 0}
        for kind, literal, rel, text in _candidates(target.source_dir, rkl, live):
            if kind in picked or tried[kind] >= _MAX_CANDIDATES:
                continue
            tried[kind] += 1
            transform = _transform_for(pct_engine, inject_rules(section, [(literal, drift_value(literal))]))
            published = transform(text, rel)
            if kind == "repo_key":
                seen = bool(rkl.drifted(text, published))
            else:
                seen =bool(live.scrubbed(text, published, lambda ident, t=transform, r=rel: t(f" {ident} ", r).strip()))
            if seen:
                picked[kind] = literal
            if len(picked) == 2:
                rules = [(lit, drift_value(lit)) for lit in (picked["repo_key"], picked["env_var"])]
                return target, inject_rules(section, rules), picked
    return None


def drift_probe(publish: Any, engine_ctx: Any, sections: list, meter: _Meter) -> DriftProbe:
    """Run both literal gates over one row with a rename rule injected for a repo-key literal and an env var."""
    from coordinator_core.percolate import engine as pct_engine
    from coordinator_core.percolate import live_identifiers as live

    rkl = publish._load_repo_key_literals()
    found = find_drift_rules(sections, pct_engine, rkl, live)
    probe = DriftProbe()
    if found is None:
        probe.detail = "no row carries both an unprotected repo-key literal and an env-var literal"
        return probe
    target, drifted_section, picked = found
    probe.repo_key, probe.env_var = picked["repo_key"], picked["env_var"]
    one = [(target, drifted_section)]
    for gate in LITERAL_GATES:
        fn = gate_calls(publish, engine_ctx, one, Path("."), Path("."))[gate]
        t0 = meter.now_ms()
        ok, _err = _quiet(fn)
        probe.process_ms[gate] = meter.now_ms() - t0
        probe.caught[gate] = not ok
    probe.detail = f"row {target.name}"
    return probe


def bar_for(gate: str, manifest: Optional[dict] = None) -> float:
    """The budget manifest's target for `gate`, falling back to the brightline when the entry is absent."""
    overrides = (manifest if manifest is not None else _budget.load_manifest()).get("overrides", {})
    entry = overrides.get(BUDGET_KEYS[gate])
    return float(entry["target_ms"]) if entry else BAR_MS


def verdict(profiles: list[GateProfile], probe: Optional[DriftProbe], manifest: Optional[dict] = None) -> bool:
    """True only when every profiled gate passed on the real tree within its bar and the probe caught both."""
    for p in profiles:
        if p.warm is None or not p.warm.ok or not p.cold or not p.cold.ok:
            return False
        if p.warm.process_ms > bar_for(p.gate, manifest):
            return False
    return probe is None or probe.passed


def render(profiles: list[GateProfile], probe: Optional[DriftProbe], instrument: str) -> str:
    lines = [f"instrument: {instrument}", f"bar: {BAR_MS:.0f} ms process time per gate, warm leg", ""]
    lines.append(f"{'gate':<18}{'cold_ms':>12}{'warm_ms':>12}{'warm_n':>8}  ok   verdict")
    for p in profiles:
        assert p.cold is not None and p.warm is not None
        ok = p.cold.ok and p.warm.ok
        state = "inadmissible (gate returned False)" if not ok else ("ok" if p.warm_within_bar else "OVER BAR")
        lines.append(
            f"{p.gate:<18}{p.cold.process_ms:>12.1f}{p.warm.process_ms:>12.1f}{p.warm.n:>8}  "
            f"{'yes' if ok else 'NO ':<4} {state}"
        )
    if probe is not None:
        lines.append("")
        lines.append(f"drift probe ({probe.detail}): repo_key={probe.repo_key!r} env_var={probe.env_var!r}")
        for gate in LITERAL_GATES:
            if gate in probe.caught:
                lines.append(
                    f"  {gate:<18} returned {'False (drift caught)' if probe.caught[gate] else 'True (DRIFT MISSED)'}"
                    f"  {probe.process_ms.get(gate, 0.0):.1f} ms"
                )
        lines.append(f"  drift probe: {'PASS' if probe.passed else 'FAIL'}")
    return "\n".join(lines)


def run(
    gates: tuple[str, ...] = GATES,
    *,
    gate_root: Optional[Path] = None,
    row_prefix: str = ROW_PREFIX,
    warm_repeat: int = 3,
    parse_only: bool = False,
    skip_drift: bool = False,
    work_dir: Path = _WORK_DIR,
    keep_work_dir: bool = False,
) -> tuple[list[GateProfile], Optional[DriftProbe], str]:
    declare_benchmark_origin()
    publish = load_publish_module()
    engine_ctx, sections = build_round(publish, row_prefix)
    if not keep_work_dir:
        shutil.rmtree(work_dir, ignore_errors=True)
    git_dir = work_dir / "git-dir"
    git_dir.mkdir(parents=True, exist_ok=True)
    root = (gate_root or _REPO).resolve()
    meter = _Meter()
    calls = gate_calls(publish, engine_ctx, sections, root, git_dir, parse_only=parse_only)
    profiles = [profile_gate(meter, g, calls[g], warm_repeat) for g in gates]
    probe = None if skip_drift else drift_probe(publish, engine_ctx, sections, meter)
    return profiles, probe, meter.instrument


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--gates", default=",".join(GATES), help=f"comma list from {','.join(GATES)}")
    parser.add_argument("--gate-root", type=Path, default=None, help="tree the function and argv gates judge (default: this repo)")
    parser.add_argument("--row-prefix", default=ROW_PREFIX)
    parser.add_argument("--warm-repeat", type=int, default=3)
    parser.add_argument("--parse-only", action="store_true", help="function gate parse sweep only (the warm-round shape)")
    parser.add_argument("--skip-drift", action="store_true")
    parser.add_argument("--keep-work-dir", action="store_true", help="reuse the work dir (a non-empty cache makes the cold leg warm)")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    gates = tuple(g for g in args.gates.split(",") if g)
    unknown = [g for g in gates if g not in GATES]
    if unknown:
        parser.error(f"unknown gate(s): {unknown}")
    try:
        profiles, probe, instrument = run(
            gates,
            gate_root=args.gate_root,
            row_prefix=args.row_prefix,
            warm_repeat=max(1, args.warm_repeat),
            parse_only=args.parse_only,
            skip_drift=args.skip_drift,
            keep_work_dir=args.keep_work_dir,
        )
    except SetupError as exc:
        print(f"publish_end_of_run_gates_profile: {exc}", file=sys.stderr)
        return EXIT_SETUP
    passed = verdict(profiles, probe)
    if args.json:
        print(json.dumps({
            "instrument": instrument,
            "bar_ms": BAR_MS,
            "gates": [
                {"gate": p.gate, "cold_ms": p.cold.process_ms, "cold_ok": p.cold.ok,
                 "warm_ms": p.warm.process_ms, "warm_ok": p.warm.ok, "warm_n": p.warm.n}
                for p in profiles if p.cold and p.warm
            ],
            "drift": None if probe is None else {
                "repo_key": probe.repo_key, "env_var": probe.env_var, "caught": probe.caught, "passed": probe.passed,
            },
            "passed": passed,
        }, indent=2))
    else:
        print(render(profiles, probe, instrument))
    return EXIT_OK if passed else EXIT_FAILED


if __name__ == "__main__":
    sys.exit(main())
