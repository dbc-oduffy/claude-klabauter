"""Per-arm, per-stage byte probe of the composed ask Workflow script, unstaged and staged, printed as JSON.

Fixtures follow the byte tests: hoisting on, runner cap lifted, m_plus via a patched `_known_arm` over a
plan-blitz padded to 279005 bytes, bare-relayed base with both blitz prompts stubbed. `--at-sha SHA` swaps the
four composer modules for their `git show SHA:` copy (cached under scratch/ask-script-bytes/SHA/) so a past
commit's script is measured against today's remaining engine; `--attribute` runs that over every commit since
the last budget set that touched a composer module. Writes nothing outside scratch/ask-script-bytes/.

Trap: only the four modules are swapped, so a commit whose composer needs a sibling module's contemporary
shape (the review roster, a contract constant) fails to import or compose and is reported as an error row, not
a measurement. Old commits are measurable only near HEAD; earlier growth needs the literal-bytes method.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Optional

_DIR = "coordinator_core/ops/dispatch_emit"
_COMPOSER_MODULES = ("ask_compose", "ask_stage_blocks", "prompt_fragments", "emit")
_BUDGET_SET_SHA = "58090f0776"
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
_BARE_ASK = "Fix: a short engine-ask without context is refused as over the Workflow script cap."
_REPO_ROOT = Path(__file__).resolve().parents[2]
_FUNC_RE = re.compile(r"^[ \t]*(?:async[ \t]+)?function[ \t]+(\w+)[ \t]*\(", re.M)


def _git(*args: str) -> str:
    return subprocess.run(
        ["git", "--no-optional-locks", "-C", str(_REPO_ROOT), *args],
        capture_output=True, text=True, encoding="utf-8", check=True, creationflags=_NO_WINDOW,
    ).stdout


def _install_sha_modules(sha: str) -> None:
    """Serve the `_COMPOSER_MODULES` from `git show sha:<path>`, cached under scratch/ask-script-bytes/<sha>/swapped/.

    A name the sha copy lacks is answered from HEAD's copy of the same module."""
    import importlib.abc
    import importlib.machinery
    import importlib.util

    cache = _REPO_ROOT / "scratch" / "ask-script-bytes" / sha / "swapped"
    cache.mkdir(parents=True, exist_ok=True)
    for mod in _COMPOSER_MODULES:
        if not (cache / f"{mod}.py").exists():
            (cache / f"{mod}.py").write_text(_git("show", f"{sha}:{_DIR}/{mod}.py"), encoding="utf-8", newline="\n")
    root = cache
    prefix = _DIR.replace("/", ".")
    head_dir = _REPO_ROOT / _DIR

    class _Loader(importlib.machinery.SourceFileLoader):
        """Source loader whose module answers a name the sha copy lacks from HEAD's copy of the same module."""

        def exec_module(self, module) -> None:
            super().exec_module(module)
            head_file = head_dir / Path(self.path).relative_to(root)
            if "__getattr__" in vars(module) or not head_file.is_file():
                return
            holder: list = []

            def _from_head(attr: str) -> object:
                if not holder:
                    spec = importlib.util.spec_from_file_location(f"_head_{module.__name__}", head_file)
                    assert spec is not None and spec.loader is not None
                    head = importlib.util.module_from_spec(spec)
                    spec.loader.exec_module(head)
                    holder.append(head)
                try:
                    return getattr(holder[0], attr)
                except AttributeError:
                    raise AttributeError(f"module {module.__name__!r} has no attribute {attr!r}") from None

            module.__getattr__ = _from_head  # type: ignore[attr-defined]

    class _Finder(importlib.abc.MetaPathFinder):
        def find_spec(self, name, path=None, target=None) -> "importlib.machinery.ModuleSpec | None":
            if not name.startswith(prefix + ".") or name[len(prefix) + 1 :] not in _COMPOSER_MODULES:
                return None
            file = root / f"{name[len(prefix) + 1 :]}.py"
            return importlib.util.spec_from_file_location(name, file, loader=_Loader(name, str(file)))

    sys.meta_path.insert(0, _Finder())


def _fixture_stack(monkey: "_Patches", plugin_root: Optional[str]) -> None:
    from coordinator_core.ops.dispatch_emit import emit, prompt_fragments

    monkey.setattr(prompt_fragments, "HOIST_ENABLED", True)
    monkey.setenv("CLAUDE_PLUGIN_ROOT", plugin_root or "")
    monkey.setattr(emit, "_WORKFLOW_SCRIPT_BYTE_CAP", 10**9)


class _Patches:
    """Minimal monkeypatch: attribute and env patches undone on `undo()`."""

    def __init__(self) -> None:
        self._undo: list = []

    def setattr(self, obj, name, value) -> None:
        old = getattr(obj, name)
        self._undo.append(lambda: setattr(obj, name, old))
        setattr(obj, name, value)

    def setenv(self, key, value) -> None:
        import os

        old = os.environ.get(key)
        self._undo.append(lambda: os.environ.pop(key, None) if old is None else os.environ.__setitem__(key, old))
        os.environ[key] = value

    def undo(self) -> None:
        while self._undo:
            self._undo.pop()()


def _size(text: str) -> int:
    return len(text.encode("utf-8"))


def _template_runs(script: str, top: int = 10) -> dict:
    """Count and bytes of every backtick template literal (the stager never touches these), with the `top` largest."""
    from coordinator_core.ops.dispatch_emit.prompt_fragments import _scan

    spans = [(s, e) for s, e in _scan(script)[1] if script[s] == "`"]
    rows = sorted(((e - s, s) for s, e in spans), reverse=True)
    return {
        "templates": len(spans),
        "bytes": sum(_size(script[s:e]) for s, e in spans),
        "top": [{"chars": n, "head": script[s : s + 80]} for n, s in rows[:top]],
    }


def inline_literal_runs(script: str, wrapped: dict[str, str], top: int = 30) -> list[dict]:
    """The `top` runs of adjacent single-quoted literals (joined by ` + `) left in the script's core, by raw bytes.

    Skips runs inside the planBlitz and roadmapBlitz blocks. `undecodable_escape` marks a run the stager refuses
    for an escape it does not decode; `enclosing_function` is the nearest preceding `function NAME(` line."""
    from coordinator_core.ops.dispatch_emit import ask_stage_budget as asb
    from coordinator_core.ops.dispatch_emit.ask_staged_prompts import _decode
    from coordinator_core.ops.dispatch_emit.prompt_fragments import _scan_literals

    skip: list[tuple[int, int]] = []
    for name in ("planBlitz", "roadmapBlitz"):
        start = script.find(f"async function {name}(")
        if start >= 0 and name in wrapped:
            carried = asb._carried_bytes(script, name, wrapped[name])
            end = start + len(script[start:].encode("utf-8")[:carried].decode("utf-8", errors="ignore"))
            skip.append((start, end))
    spans = [(s, e) for s, e in _scan_literals(script) if script[s] == "'" and not any(a <= s < b for a, b in skip)]
    runs: list[list[tuple[int, int]]] = []
    for span in spans:
        if runs and script[runs[-1][-1][1] : span[0]] == " + ":
            runs[-1].append(span)
        else:
            runs.append([span])
    funcs = [(m.start(), m.group(1)) for m in _FUNC_RE.finditer(script)]
    rows = []
    for run in runs:
        s, e = run[0][0], run[-1][1]
        func = next((n for pos, n in reversed(funcs) if pos < s), None)
        rows.append({
            "bytes": _size(script[s:e]),
            "literals": len(run),
            "head": script[s : s + 80],
            "enclosing_function": func,
            "undecodable_escape": any(_decode(script[a + 1 : b - 1]) is None for a, b in run),
        })
    rows.sort(key=lambda r: (-int(str(r["bytes"])), str(r["head"])))
    return rows[:top]


def inline_literal_totals(script: str, wrapped: dict[str, str]) -> dict:
    """Count and bytes of every inline single-quoted run left in core, by size band, and the bytes the stager cannot decode."""
    rows = inline_literal_runs(script, wrapped, top=10**9)
    bands = {"ge_600": 0, "200_599": 0, "lt_200": 0}
    for r in rows:
        b = int(str(r["bytes"]))
        bands["ge_600" if b >= 600 else "200_599" if b >= 200 else "lt_200"] += b
    return {
        "runs": len(rows),
        "bytes": sum(int(str(r["bytes"])) for r in rows),
        "bytes_by_band": bands,
        "undecodable_escape_bytes": sum(int(str(r["bytes"])) for r in rows if r["undecodable_escape"]),
    }


def _compose_arm(arm: Optional[str], staged: bool, plan_blitz_text: str, patches: _Patches) -> tuple[str, dict]:
    from coordinator_core.ops.dispatch_emit import ask_compose

    if arm:
        patches.setattr(ask_compose, "_known_arm", lambda *_: arm)
    sink: dict = {}
    kw: dict = {"plan_blitz_text": plan_blitz_text} if plan_blitz_text else {}
    if staged:
        kw["staged_out"] = sink
    script = ask_compose.compose_ask_script(
        repo_root=str(_REPO_ROOT), prompt="probe ask", sizing_rel=None,
        run_id="ask-probe-000000", session_id="probe", **kw,
    )
    return script, sink


def measure(literals: bool = True) -> dict:
    """Totals, `asb.stage_bytes` split, staged-sink bytes and bare-relayed base for both arms, unstaged and staged."""
    from coordinator_core.ops.dispatch_emit import ask_stage_budget as asb
    from coordinator_core.warm.caller_context import resolve_caller_context
    from coordinator_core.ops.dispatch_emit.tests.test_ask_script_byte_headroom import _padded_plan_blitz_text

    plugin_root = resolve_caller_context().plugin_root
    patches = _Patches()
    out: dict = {"cap": 524288, "arms": {}}
    try:
        _fixture_stack(patches, plugin_root)
        padded = _padded_plan_blitz_text()
        out["padded_plan_blitz_bytes"] = _size(padded)
        for arm_key, arm, text in (("bare", None, ""), ("m_plus", "m_plus", padded)):
            wrapped = asb.stage_texts(plan_blitz_text=text or None)
            arm_out: dict = {}
            for shape, staged in (("unstaged", False), ("staged", True)):
                p = _Patches()
                try:
                    script, sink = _compose_arm(arm, staged, text, p)
                except TypeError as exc:
                    if not staged or "staged_out" not in str(exc):
                        raise
                    continue
                finally:
                    p.undo()
                row = {"total": _size(script), "stages": asb.stage_bytes(script, wrapped)}
                if staged:
                    row["sink_files"] = len(sink)
                    row["sink_bytes"] = sum(_size(v) for v in sink.values())
                    if literals and arm_key == "m_plus":
                        row["top_inline_literal_runs"] = inline_literal_runs(script, wrapped)
                        row["inline_literal_totals"] = inline_literal_totals(script, wrapped)
                        row["template_literals"] = _template_runs(script)
                arm_out[shape] = row
            out["arms"][arm_key] = arm_out
        out["production_m_plus"] = production_m_plus(padded)
        out["bare_relayed_base"] = _bare_relayed_base(patches)
        out["budgets"] = {k: dict(v) for k, v in asb.STAGE_BUDGETS.items()}
        out["checks"] = {
            "m_plus_staged_total_le_cap": out["arms"]["m_plus"]["staged"]["total"] <= 524288,
            "bare_relayed_base_le_310000": out["bare_relayed_base"].get("staged", out["bare_relayed_base"]["unstaged"]) <= 310000,
            "stage_budget_failures_staged": {
                arm: asb.budget_failures(arm, out["arms"][arm]["staged"]["stages"]) for arm in ("bare", "m_plus")
            },
            "stage_budget_failures_unstaged": {
                arm: asb.budget_failures(arm, out["arms"][arm]["unstaged"]["stages"]) for arm in ("bare", "m_plus")
            },
        }
    finally:
        patches.undo()
    return out


def production_m_plus(padded: str) -> dict:
    """The m_plus shapes production composes: a known m_plus sizing (parent) and its chain-hop launcher child, both staged.

    Unlike the `_known_arm`-patched unsized fixture, these carry no in-run size stage; the pad is the same 279005 bytes.
    Each row holds the total, the `asb.stage_bytes` split and whether the script is within the cap less 4096."""
    from coordinator_core.ops.dispatch_emit import ask_compose, ask_stage_budget as asb
    from coordinator_core.ops.dispatch_emit.tests.test_ask_compose_script_size_budget import _SIZING_REL, _write_sizing
    from coordinator_core.temp_layout import managed_temp_dir

    wrapped = asb.stage_texts(plan_blitz_text=padded)
    out: dict = {}
    with managed_temp_dir("ask-bytes-probe-m-plus-") as tmp:
        _write_sizing(Path(tmp))
        for name, kw in (("parent", {}), ("chain_child", {"launcher_child": True, "chain_hop": True, "in_run_commit": True})):
            sink: dict = {}
            script = ask_compose.compose_ask_script(
                repo_root=str(tmp), prompt=None, sizing_rel=_SIZING_REL, run_id="run-1", session_id=None,
                script_path="scratch/warp/x.mjs", plan_blitz_text=padded, staged_out=sink, **kw,
            )
            total = _size(script)
            out[name] = {
                "arm": ask_compose._known_arm(str(tmp), _SIZING_REL),
                "total": total,
                "stages": asb.stage_bytes(script, wrapped),
                "within_cap_less_4096": total <= 524288 - 4096,
            }
    return out


def _bare_relayed_base(patches: _Patches) -> dict:
    """Bare relayed ask with both blitz prompts stubbed over an empty repo root, as `_base_bytes` in the size-budget test."""
    from coordinator_core import machine_resolver
    from coordinator_core.bash_guards._heavy_admission_contract import KEY_VITEST_MAX_WORKERS
    from coordinator_core.ops.dispatch_emit import ask_compose
    from coordinator_core.ops.dispatch_emit.tests.test_ask_compose_raw_script_under_cap import _STUB_BLITZ, _STUB_ROADMAP
    from coordinator_core.temp_layout import managed_temp_dir

    real = machine_resolver.registry_get
    patches.setattr(
        machine_resolver, "registry_get", lambda key: 4 if key == KEY_VITEST_MAX_WORKERS else real(key)
    )
    out = {}
    with managed_temp_dir("ask-bytes-probe-") as tmp:
        for shape, kw in (("unstaged", {}), ("staged", {"staged_out": {}})):
            try:
                script = ask_compose.compose_ask_script(
                    repo_root=str(tmp), session_id=None, script_path="scratch/warp/x.mjs", prompt=_BARE_ASK,
                    sizing_rel=None, run_id="run-1", ask_source="relayed", **kw, **_STUB_BLITZ, **_STUB_ROADMAP,
                )
            except TypeError as exc:
                if not kw or "staged_out" not in str(exc):
                    raise
                continue
            out[shape] = _size(script)
    return out


def _commits() -> list[str]:
    paths = [f"{_DIR}/{m}.py" for m in _COMPOSER_MODULES]
    return _git("log", "--reverse", "--format=%h", f"{_BUDGET_SET_SHA}..HEAD", "--", *paths).split()


def literal_delta() -> list[dict]:
    """Per commit since the budget-set commit, bytes of quoted-string source lines added and removed in the composer modules.

    One `git log -p -U0` spawn. A source-side proxy for script growth where `--at-sha` cannot import the commit:
    a quoted line is counted whole, so it over-counts code that carries a literal and misses template text."""
    paths = [f"{_DIR}/{m}.py" for m in _COMPOSER_MODULES]
    text = _git("log", "-p", "-U0", "--no-color", "--format=@@COMMIT %h", f"{_BUDGET_SET_SHA}..HEAD", "--", *paths)
    rows: list[dict] = []
    for chunk in text.split("@@COMMIT ")[1:]:
        sha, _, body = chunk.partition("\n")
        by_file: dict[str, dict[str, int]] = {}
        current = ""
        for line in body.splitlines():
            if line.startswith("+++ b/"):
                current = line[6:].rsplit("/", 1)[-1]
            elif line.startswith(("+", "-")) and not line.startswith(("+++", "---")) and ("'" in line or '"' in line):
                per = by_file.setdefault(current, {"added": 0, "removed": 0})
                per["added" if line[0] == "+" else "removed"] += _size(line[1:]) + 1
        added = sum(p["added"] for p in by_file.values())
        removed = sum(p["removed"] for p in by_file.values())
        rows.append({"sha": sha.strip(), "added": added, "removed": removed, "net": added - removed, "by_file": by_file})
    return rows


def attribute() -> dict:
    """Probe the script at the budget-set commit and each commit since that touched a composer module; rows carry deltas."""
    shas = [_BUDGET_SET_SHA, *_commits()]
    rows, prev = [], None
    for sha in shas:
        proc = subprocess.run(
            [sys.executable, "-m", "coordinator_core.benchmarks.ask_script_bytes_probe", "--at-sha", sha, "--no-literals"],
            capture_output=True, text=True, encoding="utf-8", cwd=str(_REPO_ROOT), creationflags=_NO_WINDOW,
        )
        try:
            m = json.loads(proc.stdout.strip().splitlines()[-1])
        except (ValueError, IndexError):
            rows.append({"sha": sha, "error": (proc.stderr or proc.stdout)[-300:]})
            continue
        cur = {
            "bare_core_unstaged": m["arms"]["bare"]["unstaged"]["stages"]["core"],
            "bare_core_staged": m["arms"]["bare"]["staged"]["stages"]["core"],
            "m_plus_core_unstaged": m["arms"]["m_plus"]["unstaged"]["stages"]["core"],
            "m_plus_core_staged": m["arms"]["m_plus"]["staged"]["stages"]["core"],
            "m_plus_planBlitz": m["arms"]["m_plus"]["unstaged"]["stages"]["planBlitz"],
            "bare_roadmapBlitz": m["arms"]["bare"]["unstaged"]["stages"]["roadmapBlitz"],
            "m_plus_total_staged": m["arms"]["m_plus"]["staged"]["total"],
            "bare_relayed_base": m["bare_relayed_base"]["unstaged"],
        }
        row = {"sha": sha, **cur}
        if prev:
            row["delta"] = {k: cur[k] - prev[k] for k in cur}
        prev = cur
        rows.append(row)
    return {"base_sha": _BUDGET_SET_SHA, "rows": rows}


def main(argv: list[str]) -> int:
    if "--at-sha" in argv:
        _install_sha_modules(argv[argv.index("--at-sha") + 1])
    if "--attribute" in argv:
        print(json.dumps(attribute()))
        return 0
    if "--literal-delta" in argv:
        print(json.dumps(literal_delta()))
        return 0
    print(json.dumps(measure(literals="--no-literals" not in argv)))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
