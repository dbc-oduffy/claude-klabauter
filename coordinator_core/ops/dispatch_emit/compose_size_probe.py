"""Probe: compose a bare relayed ask under production inputs and print each inlined component's bytes as JSON."""

from __future__ import annotations

import collections
import json
import re
import sys
from unittest import mock

from coordinator_core.ops.dispatch_emit import ask_compose, ask_plan_blitz, ask_roadmap, emit, prompt_fragments
from coordinator_core.ops.dispatch_emit.compose_size import component_sizes
from coordinator_core.session import record_homes
from coordinator_core.warm.caller_context import resolve_caller_context

_STUB_BLITZ = dict(
    wrap_stage=lambda _t, **_k: ("  async function planBlitz(args) {\n    return { ready: [] };\n  }", ["Size", "Plan"]),
    plan_blitz_text="stub",
)
_STUB_ROADMAP_TEXT = "export const meta = { name: 'rb', phases: [{ title: 'Synthesize', detail: 'x' }] };\nreturn {};"
_ASK = "Fix: a short engine-ask without context is refused as over the Workflow script cap."


def _compose(repo_root: str, ask: str = _ASK, **stubs) -> str:
    return ask_compose.compose_ask_script(
        repo_root=repo_root,
        prompt=ask,
        sizing_rel=None,
        run_id="run-1",
        session_id=None,
        script_path="x.mjs",
        ask_source="relayed",
        **stubs,
    )


def measure(repo_root: str) -> dict:
    plugin_root = resolve_caller_context().plugin_root
    assets = dict(
        plan_blitz_text=ask_plan_blitz.load_plan_blitz_text(plugin_root),
        roadmap_blitz_text=ask_roadmap.load_roadmap_blitz_text(plugin_root),
    )
    from coordinator_core import machine_resolver
    from coordinator_core.bash_guards._heavy_admission_contract import KEY_VITEST_MAX_WORKERS

    patched = machine_resolver.registry_get
    with (
        mock.patch.object(prompt_fragments, "HOIST_ENABLED", True),
        mock.patch.dict("os.environ", {"CLAUDE_PLUGIN_ROOT": plugin_root or ""}),
        mock.patch.object(
            machine_resolver, "registry_get", lambda key: 4 if key == KEY_VITEST_MAX_WORKERS else patched(key)
        ),
        mock.patch.object(emit, "_WORKFLOW_SCRIPT_BYTE_CAP", 10**9),
    ):
        full = _compose(repo_root, **assets)
        no_blitz = _compose(repo_root, **_STUB_BLITZ, roadmap_blitz_text=assets["roadmap_blitz_text"])
        base = _compose(repo_root, **_STUB_BLITZ, roadmap_blitz_text=_STUB_ROADMAP_TEXT)
    size = lambda s: len(s.encode("utf-8"))
    pm_lines = [ln for ln in full.splitlines() if "pm_turns" in ln]
    parts = {
        "plan_blitz_stage": "x" * (size(full) - size(no_blitz)),
        "roadmap_blitz_stage": "x" * (size(no_blitz) - size(base)),
        "base_script": base,
    }
    return {
        "total": size(full),
        "cap": emit._WORKFLOW_SCRIPT_BYTE_CAP,
        "components": component_sizes(parts),
        "pm_turns_mention_lines": {"lines": len(pm_lines), "bytes": sum(size(ln) + 1 for ln in pm_lines)},
    }


PAIRED_ASK = "Fix: a short engine-ask without context is refused as over the Workflow script cap. " * 14
PAIRED_CONTEXT = ("docs/a.md", "docs/b.md", "docs/c.md", "docs/d.md")


def shape_delta(left: str, right: str) -> dict:
    """Totals of two composed scripts, their byte delta, and the lines that differ ranked by size via `component_sizes`."""
    import difflib

    a, b = left.splitlines(), right.splitlines()
    parts: dict[str, str] = {}
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
        if tag != "equal":
            parts[f"left[{i1}:{i2}]->right[{j1}:{j2}]"] = "\n".join(a[i1:i2] + b[j1:j2])
    return {
        "left": len(left.encode("utf-8")),
        "right": len(right.encode("utf-8")),
        "delta": len(right.encode("utf-8")) - len(left.encode("utf-8")),
        "differing_components": component_sizes(parts)[:5],
    }


def measure_paired(repo_root: str, ask: str = PAIRED_ASK, context: tuple[str, ...] = PAIRED_CONTEXT) -> dict:
    """Compose a relayed ask with a brief carrying no context, then the same brief plus `context`; report totals and delta.

    The brief text is the same in both, so the delta is the context clause alone."""
    from coordinator_core.ops.dispatch_emit.ask_brief import EmBrief, brief_decl_js

    plugin_root = resolve_caller_context().plugin_root
    assets = dict(
        plan_blitz_text=ask_plan_blitz.load_plan_blitz_text(plugin_root),
        roadmap_blitz_text=ask_roadmap.load_roadmap_blitz_text(plugin_root),
    )
    from coordinator_core import machine_resolver
    from coordinator_core.bash_guards._heavy_admission_contract import KEY_VITEST_MAX_WORKERS

    patched = machine_resolver.registry_get
    plain = EmBrief(text="Frame: the plan", source="inline", context=())
    with_ctx = EmBrief(text=plain.text, source="inline", context=tuple(context))
    with (
        mock.patch.object(prompt_fragments, "HOIST_ENABLED", True),
        mock.patch.dict("os.environ", {"CLAUDE_PLUGIN_ROOT": plugin_root or ""}),
        mock.patch.object(
            machine_resolver, "registry_get", lambda key: 4 if key == KEY_VITEST_MAX_WORKERS else patched(key)
        ),
        mock.patch.object(emit, "_WORKFLOW_SCRIPT_BYTE_CAP", 10**9),
    ):
        bare = _compose(repo_root, ask, **assets)
        left = _compose(repo_root, ask, em_brief=plain, **assets)
        right = _compose(repo_root, ask, em_brief=with_ctx, **assets)
    out = shape_delta(left, right)
    out["bare_total"] = len(bare.encode("utf-8"))
    out["brief_framing_bytes"] = out["left"] - out["bare_total"]
    out["context_bytes"] = len(brief_decl_js(with_ctx).encode("utf-8")) - len(brief_decl_js(plain).encode("utf-8"))
    return out


def measure_inputs() -> dict:
    """Raw bytes of each compose input read from disk or state, and which one explains the bare-vs-context gap.

    Only the workflow assets grow between releases; the rest are fixed-size configs. The bare form carries
    no `_EM_BRIEF` prefix, so it is smaller than a context-list form by the brief framing alone."""
    from coordinator_core.ops.dispatch_emit import op
    from coordinator_core.ops.dispatch_emit.emission_receipt import _load_review_inputs

    plugin_root = resolve_caller_context().plugin_root
    size = lambda v: len((v if isinstance(v, str) else json.dumps(v, default=str)).encode("utf-8"))
    fragment, schemas = _load_review_inputs("plan")
    plan_blitz = ask_plan_blitz.load_plan_blitz_text(plugin_root)
    roadmap = ask_roadmap.load_roadmap_blitz_text(plugin_root)
    inputs = {
        "plan-blitz.mjs (raw)": size(plan_blitz),
        "roadmap-blitz.mjs (raw)": size(roadmap),
        "review roster fragment": size(fragment),
        "review stage schemas": size(schemas),
        "plan_blitz_args": size(op._resolve_plan_blitz_args(None)),
    }
    return {
        "inputs": inputs,
        "asset_cap": ask_compose.STAGE_ASSET_BYTE_CAP,
        "growing_input": max(inputs, key=inputs.get),
        "bare_vs_context": "bare omits the _EM_BRIEF framing; see brief_framing_bytes in the paired measurement",
    }


_LITERAL_RE = re.compile(r"'(?:[^'\\\n]|\\.){40,}'|\"(?:[^\"\\\n]|\\.){40,}\"|`(?:[^`\\]|\\.){40,}`", re.S)


def repeated_literals(script: str, top: int = 20) -> list[dict]:
    """The `top` string literals of 40+ chars that occur more than once, ranked by bytes x count."""
    counts = collections.Counter(m.group(0) for m in _LITERAL_RE.finditer(script))
    rows = [
        {"bytes": len(lit.encode("utf-8")), "count": n, "total": len(lit.encode("utf-8")) * n, "head": lit[:80]}
        for lit, n in counts.items()
        if n > 1
    ]
    return sorted(rows, key=lambda r: (-r["total"], r["head"]))[:top]


def measure_headroom(repo_root: str) -> dict:
    """Raw ask and launcher-child (staged refire twin, slot unfilled) composed at default arguments with production assets.

    Reports totals, percent of `emit._WORKFLOW_SCRIPT_BYTE_CAP`, the `measure` component split and the top 20 repeated literals."""
    plugin_root = resolve_caller_context().plugin_root
    assets = dict(
        plan_blitz_text=ask_plan_blitz.load_plan_blitz_text(plugin_root),
        roadmap_blitz_text=ask_roadmap.load_roadmap_blitz_text(plugin_root),
    )
    with mock.patch.object(emit, "_WORKFLOW_SCRIPT_BYTE_CAP", 10**9):
        raw = _compose(repo_root, **assets)
        twin = _compose(repo_root, launcher_child=True, **assets)
    cap = emit._WORKFLOW_SCRIPT_BYTE_CAP
    size = lambda s: len(s.encode("utf-8"))
    return {
        "raw_total": size(raw),
        "staged_refire_total": size(twin),
        "cap": cap,
        "raw_pct": round(100 * size(raw) / cap, 2),
        "staged_pct": round(100 * size(twin) / cap, 2),
        "components": measure(repo_root)["components"],
        "top_repeated_literals": repeated_literals(raw),
    }


_REFUSAL_RE = re.compile(r"(\d+) bytes(?:; largest inlined component (.+?) \((\d+) bytes\))?")
_ROADMAP_SIZING_REL = record_homes.home_rel("sizings") + "/probe-roadmap.yaml"
#: Share of the Workflow runner byte cap a composed script may use; the margin ratchet and this probe both read it.
CEILING_RATIO = 0.90


def parse_cap_refusal(text: str) -> dict:
    """`total` (and the largest inlined component, when named) out of a `compose_size.cap_refusal` message."""
    m = _REFUSAL_RE.search(text.split("byte cap:", 1)[-1])
    if m is None:
        raise ValueError(f"not a cap refusal: {text[:120]}")
    out = {"total": int(m.group(1)), "refused": True}
    if m.group(2):
        out["largest_component"] = {"name": m.group(2), "bytes": int(m.group(3))}
    return out


def compose_bytes(compose) -> dict:
    """Run `compose()` and report its bytes: `len(script.encode("utf-8"))`, or the total a cap refusal names."""
    try:
        script = compose()
    except ask_compose.AskComposeRefused as exc:
        return parse_cap_refusal(str(exc))
    return {"total": len(script.encode("utf-8")), "refused": False}


def measure_roadmap_fanout(repo_root: str) -> dict:
    """Standalone roadmap-fanout and in-run M+ re-fire composed with production assets and the real cap.

    The cap is left in force, so an over-cap variant reports the total its refusal names. Totals differ
    from the stub-asset ratchet; this answers which component grew."""
    import os
    import tempfile
    from pathlib import Path

    import yaml

    from coordinator_core.ops.dispatch_emit.emission_receipt import _load_review_inputs

    plugin_root = resolve_caller_context().plugin_root
    fragment, schemas = _load_review_inputs("plan")
    assets = dict(
        plan_blitz_text=ask_plan_blitz.load_plan_blitz_text(plugin_root),
        roadmap_blitz_text=ask_roadmap.load_roadmap_blitz_text(plugin_root),
        review_roster_fragment=fragment,
        review_stage_schemas=schemas,
        run_base_sha="a" * 40,
    )
    doc = {
        "schema": "sizing-object", "name": "rm", "intent": "an XL job",
        "estimate": {"tshirt": "XL", "provisional": True}, "route": "roadmap",
        "detents": [], "fork": None, "xl_exit": None, "status": "sized",
        "premise": {"provenance": "not-applicable", "evidence": "probe"},
        "deliverable_id": "dlv-probe-abc123", "interaction_mode": "pm",
        "exit_criterion": {"statement": "done", "accepted": {"pm_quote": "yes", "on": "2026-10-02", "mode": "pm"}},
    }
    cwd = os.getcwd()
    with tempfile.TemporaryDirectory() as tmp:
        sizing = Path(tmp) / _ROADMAP_SIZING_REL
        sizing.parent.mkdir(parents=True)
        sizing.write_text(yaml.safe_dump(doc), encoding="utf-8", newline="\n")
        os.chdir(tmp)
        try:
            with (
                mock.patch.object(prompt_fragments, "HOIST_ENABLED", True),
                mock.patch.dict("os.environ", {"CLAUDE_PLUGIN_ROOT": plugin_root or ""}),
            ):
                standalone = compose_bytes(
                    lambda: ask_compose.compose_ask_script(
                        repo_root=Path(tmp).as_posix(), prompt=None, sizing_rel=_ROADMAP_SIZING_REL,
                        run_id="probe-1", session_id=None, **assets,
                    )
                )
                with mock.patch.object(ask_compose, "_known_arm", lambda *_: ask_compose.ARM_M_PLUS):
                    in_run = compose_bytes(
                        lambda: ask_compose.compose_ask_script(
                            repo_root=repo_root, prompt="add a thing", sizing_rel=None,
                            run_id="probe-2", session_id=None, in_run_commit=True, **assets,
                        )
                    )
        finally:
            os.chdir(cwd)
    cap = emit._WORKFLOW_SCRIPT_BYTE_CAP
    ceiling = int(CEILING_RATIO * cap)
    return {"cap": cap, "ceiling": ceiling, "standalone": standalone, "in_run": in_run}


def main() -> int:
    from pathlib import Path

    root = str(Path(ask_compose.__file__).resolve().parents[3])
    if "--roadmap-fanout" in sys.argv[1:]:
        print(json.dumps(measure_roadmap_fanout(root)))
        return 0
    if "--headroom" in sys.argv[1:]:
        print(json.dumps(measure_headroom(root)))
        return 0
    print(json.dumps(measure(root)))
    paired = measure_paired(root)
    print(json.dumps(paired))
    print(json.dumps(measure_inputs()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
