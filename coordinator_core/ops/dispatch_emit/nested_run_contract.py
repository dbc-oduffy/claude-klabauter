"""The launcher <-> run-script seam for a multi-ask fan-out.

A launcher script starts each emitted run as a nested workflow child. A nested child receives no
args (typeof args is 'undefined', run wf_35910dfa-28a) and nesting is one level deep, so a child
never calls ``workflow()`` and cannot be handed a manifest. The seam is the child's
``stage_pending`` return plus a staged twin: a copy of the run script with ``STAGED_INLINE_SLOT``
replaced by the sha-verified manifest text, at ``staged_twin_rel(run_id, sha256)``. Pure
constants and one pure string function: no I/O, no imports.
"""

# dispatch.emit param the launcher's emit leg sets: renders the script for a launcher to start (no workflow() reader).
LAUNCHER_CHILD_EMIT_PARAM = "launcher_child"

# A launcher child's return when it reaches a stage fold: {handle, ...}.
STAGE_PENDING_KEY = "stage_pending"

# A launcher child's return when its sizing resolved M+: the dispatch.emit params of the {sizing} script the launcher emits and starts once.
CHAIN_HANDOFF_KEY = "chain"

# The one line the twin writer replaces with the verified manifest text.
STAGED_INLINE_SLOT = "const _STAGED_INLINE = null;"

# The inline payload's resume block tells a twin it is past stage and carries what the launcher's own legs settled.
STAGED_RESUME_KEY = "resume"
STAGED_RESUME_FIELDS = ("arm", "sizing_rel", "plan_rel", "tshirt")


# Key the slim inline manifest carries naming the on-disk spill (rel path) holding the per-row bulk data.
STAGED_SPILL_KEY = "spill"

# Row keys the launcher-child script schedules on; they stay inline. `writes_count` replaces the
# `writes` array. Trap: `writes` / `writes_under` are NOT here, and a slim row's absent list must
# never reach a script consumer as an empty declared set (it scopes a commit to nothing).
SLIM_ROW_KEYS = (
    "id", "brief_path", "agent_type", "model", "wave", "deps", "group", "kept_from", "route", "writes_count",
)

# Per-row list keys that move to the spill; in-script readers of these (commit leg, footprint, halt write-set) resolve the full list from it.
SPILLED_ROW_KEYS = ("writes", "writes_under")

# A packed twin carries the manifest text with its repeated JSON strings replaced by `<mark><base36 index><mark>`
# beside `{m: mark, t: [string literals]}` under this key; `STAGED_UNPACK_JS` rebuilds the byte-identical text, so
# the handle stays the full manifest's and every in-script reader sees the whole manifest, with no spill.
STAGED_PACK_KEY = "pack"
STAGED_UNPACK_JS = (
    "function _unpackStaged(o) { const p = o.pack; const a = o.text.split(p.m); "
    "for (let i = 1; i < a.length; i += 2) a[i] = p.t[parseInt(a[i], 36)]; "
    "const r = { ...o }; delete r.pack; r.text = a.join(''); return r; }"
)

# Head of the refusal when a staged twin exceeds the Workflow runner cap (distinct from the relay-corrupt head).
STAGED_TWIN_OVER_CAP_HEAD = "staged twin over the Workflow runner cap"


# Suffix of an emitted run script; the JS readers splice this constant rather than restate it.
RUN_SCRIPT_SUFFIX = ".workflow.mjs"


def twin_run_id(script_rel: str) -> str | None:
    """Run id the staged twin is written under, from the started script path.

    The twin writer reads ``<run_id>.workflow.mjs``, so this stem IS the writer's run id. None when
    the basename lacks the suffix or is a staged twin itself.

    Python reference for the stem rule the generated JS (`fan_launcher._startLoopRun`,
    `ask_compose._chain_stage_loop_js`) restates inline; it has no other production caller.
    """
    base = script_rel.replace("\\", "/").rsplit("/", 1)[-1]
    if not base.endswith(RUN_SCRIPT_SUFFIX):
        return None
    stem = base[: -len(RUN_SCRIPT_SUFFIX)]
    if not stem or ".staged-" in stem:
        return None
    return stem


def staged_twin_rel(run_id: str, sha256: str) -> str:
    """Repo-relative path of the staged twin; callers validate run_id and sha256 (64 lowercase hex)."""
    return f"scratch/warp/{run_id}.staged-{sha256}.workflow.mjs"


def staged_spill_rel(run_id: str, sha256: str) -> str:
    """Repo-relative path of the per-row spill beside the twin; same validation duty as `staged_twin_rel`."""
    return f"scratch/warp/{run_id}.staged-{sha256}.spill.json"
