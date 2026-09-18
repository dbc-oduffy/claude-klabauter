"""coordinator_core.hooks.emit_effective_delivery -- the x-effective-delivery
manifest generator.

Arrival note (W4-C7, docs/plans/2026-09-18-doe-holds-no-scripts.md): ported
from DoE-claude `coordinator/hooks/scripts/emit_effective_delivery.py`. Same
job as the source: build the `x-effective-delivery` block
`coordinator_core.ops.session.hook_delivery_manifest` (and, upstream of it,
`guard_settings_integrity.detect_hook_delivery_duplication`) reads at boot,
and write it to the sidecar those readers already resolve --
`<doe content root>/hooks/effective-delivery.json` (see
`coordinator_core.ops.session.guard_settings_integrity._EFFECTIVE_DELIVERY_REL`
and `check_generator_output_staleness.py`'s own artifact path) -- UNCHANGED
by this port: those readers still live DoE-side and still resolve that path
relative to the DoE plugin content root, never this repo.

WHAT CHANGED -- the cross-plane reads. DoE's source ran DoE-resident and
needed `_engine_root.resolve_claude_klabauter_root()` to jump INTO this engine for
`coordinator_core.write_guards.engine.discover_guard_names()` and
`coordinator_core.bash_guards.guard_roster()`. This module now runs
ENGINE-resident (same reasoning as `preuse_write_dispatch.py`'s own port
note): those two reads are ordinary same-repo imports, no `sys.path`
surgery, no `_engine_root` seam, no `_import_engine_module()` helper.

WHAT CHANGED -- resolving `hooks.json` itself. `hooks.json` is the ONE
artifact this generator reads that stays DoE-resident (it is the plugin's
own hook-registration manifest) -- so this is now the one remaining
cross-plane read, direction reversed from the source: DoE-resident code
used to resolve INTO the engine; this engine-resident code now resolves
OUT to DoE. Resolved via `coordinator_core.doe_root_pointer.
read_doe_root_pointer()` + `coordinator_core.data_root.content_root_for()`
-- the same registry-first/pointer-file/content-root seam
`oss_operative_strings._resolve_mcp_topology_path` (W4-C6) already
established for the identical displacement class, mirrored here rather
than re-invented. Fails closed (`EmitterError`) on any miss, matching this
module's own fail-closed posture for every other read.

WHAT CHANGED -- `_fanin_registries` -> `coordinator_core.hooks.
fanin_registries`, an ordinary same-repo package import (W4-C7's own
sibling write), never a `spec_from_file_location` load off a bare
filename.

`_hook_boot` (the row's own body note): `_TRAMPOLINE_TAILS` below is
UNCHANGED from the source -- it names `scripts/_hook_boot.py` and
`scripts/_hook_venv_inject.py` as non-registration trampoline tails to
skip while walking `hooks.json`'s `args` arrays, the same read the source
always did. This module never imports `_hook_boot.py` as a Python module
(the source did not either); "registration model" names what that file's
PRESENCE in an `args` array means to THIS walk -- a bootstrap tail, not a
guard -- and the port's read of that fact is a read of `hooks.json`'s own
registration data (the `args` array token), never a read of `_hook_boot.py`
itself. W4-C16 (the `hook-run` command-door landing, out of this row's
scope) may retire `_hook_boot.py`/change the registered token shape
entirely; until that chunk lands, `hooks.json` still carries the shape
this constant partitions, so it is carried forward verbatim rather than
guessed at ahead of that chunk's own landing.

WHAT CHANGED -- emission provenance. The source's `PROVENANCE_KEYS` split
one repo-side triple (DoE, hosting the generator) from one engine-side
triple (claude-klabauter, cross-plane, read via `_engine_source_provenance()`)
because the generator and the engine it reported on were two different
repos. They are now the SAME repo for two of the three engine-side facts
(`write_guards.engine`, `bash_guards` are same-repo reads), so
`_engine_source_provenance()` and its three `engine_source_*` keys are
RETIRED -- `coordinator_core.ops.check_generator_output_staleness` (the
one documented consumer of this block's provenance keys) reads only
`generated_from_sha`/`generated_from_dirty_tree`, generically, against
whichever repo hosts the generator; it has no `engine_source_*`
call site. What remains genuinely cross-plane is `hooks.json` itself
(DoE-resident) -- `_doe_source_provenance()` replaces
`_engine_source_provenance()` for exactly that one fact, renamed to match
what it now names (the DoE tree `hooks.json` was actually read from, not
"the engine").

Negative spec (unchanged from the source): does not seed `tool_names`
uniformly, does not assert `tool_names` against the live carrier-level
`matcher`, does not transcribe `list_ported_advisory_ops()`'s six-name
tuple as the advisory carrier's op list, does not call a private
`_build_guard_chain`, does not re-derive guard identity by
filename-globbing, never uses the word "filename" for the tail-key join
field, and (new to this port) never writes into this repo -- `write_block`
still targets the resolved DoE content root, never a path under this
checkout.

Spec backlink: docs/plans/2026-09-18-doe-holds-no-scripts.md § W4-C7
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from collections import namedtuple
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path, PureWindowsPath
from typing import Any, Dict, List, Optional, Set, Tuple

from coordinator_core.hooks import fanin_registries as _fanin_registries

REPO_ROOT = Path(__file__).resolve().parents[2]

#: The manifest's home is resolved lazily (`_resolve_manifest_paths()`) --
#: writing into a DoE content root is a cross-repo fact that must fail
#: closed at generation time, not at import time.
MANIFEST_REL = ("hooks", "effective-delivery.json")
HOOKS_JSON_REL = ("hooks", "hooks.json")
MANIFEST_KEY = "x-effective-delivery"

MAX_STRING_LEN = 200

#: The two emission-provenance keys `build_block()` adds beside `version`
#: for THIS repo, plus the one renamed cross-plane fact
#: (`doe_source_*`, see module docstring's "WHAT CHANGED -- emission
#: provenance"). Named here as the single source of truth so
#: `render_block`'s content-idempotence contract and this suite's own
#: tests strip exactly this set, never a hand-typed tuple that can
#: silently desync.
PROVENANCE_KEYS = (
    "generated_from_sha",
    "generated_at",
    "generated_from_dirty_tree",
    "doe_source_sha",
    "doe_source_dirty_tree",
)

_GIT_TIMEOUT_SECONDS = 10

#: Bootstrap trampoline tails -- present at the tail of every hooks.json
#: `args` array, never a registration in their own right. See module
#: docstring's "`_hook_boot` (the row's own body note)" section: carried
#: forward unchanged from the source until W4-C16 (out of this row's
#: scope) changes what `hooks.json` actually registers.
_TRAMPOLINE_TAILS = frozenset({"scripts/_hook_venv_inject.py", "scripts/_hook_boot.py"})

#: The four carrier tail keys this plan's delivery graph names, expressed
#: as the raw pre-substitution hooks.json token each carrier's OWN
#: top-level entry declares -- fed through `tail_key()` below, never
#: hand-typed as a tail string, so a future rename of any of the four
#: cannot silently desync the constant from the actual join key.
_CARRIER_RAW_TOKENS = {
    "write_dispatch": "${CLAUDE_PLUGIN_ROOT}/hooks/scripts/preuse-write-dispatch.py",
    "stop_family": "${CLAUDE_PLUGIN_ROOT}/hooks/scripts/postuse-stop-family-dispatch.py",
    "bash_dispatch": "${CLAUDE_PLUGIN_ROOT}/hooks/scripts/preuse-bash-dispatch.py",
    "advisory_dispatch": "${CLAUDE_PLUGIN_ROOT}/hooks/scripts/postuse-advisory-dispatch.py",
}

#: `coordinator_core.hooks` MODULE names (this package's own
#: `_fanin_registries.FANIN_DISPATCHERS` keys, never the DoE-side
#: hyphenated filenames `_CARRIER_RAW_TOKENS` carries) for the carriers
#: that have a DEDICATED builder above merging cross-plane engine-sourced
#: guards, which `build_carrier_fanin`'s doctrine-plane-only walk has no
#: way to see. `build_carrier_fanin` MUST skip any dispatcher named here.
#: Down-selected to the three carriers landed in this repo today (see
#: `_fanin_registries`'s own module docstring) -- `write_dispatch` ->
#: `preuse_write_dispatch`, `stop_family` -> `stop_dispatch`,
#: `advisory_dispatch` -> `postuse_advisory_dispatch`. `bash_dispatch` has
#: no `FANIN_DISPATCHERS` counterpart (it is not a SessionStart/Stop
#: fan-in dispatcher) and is intentionally absent from this set.
_DEDICATED_BUILDER_FANIN_FILENAMES = frozenset(
    {"preuse_write_dispatch", "stop_dispatch", "postuse_advisory_dispatch"}
)

#: The five confirmed retirees -- declared data, not discoverable by
#: scanning hooks.json.
_RETIRED = (
    (
        "nudge-foreground-agent-dispatch.py",
        "Deregistered from hooks.json; folded into a pure-Python port inside enforce-agent-dispatch-mode.py that makes no engine-plane call at all.",
    ),
    (
        "runtime-tripwire-stop-watcher.py",
        "Stood down 2026-07-31 per PM ruling; reversible, comment-only mention remains in hooks.json.",
    ),
    (
        "guard-named-dispatch-tool-restriction.py",
        "Deregistered from hooks.json; folded into a pure-Python port inside enforce-agent-dispatch-mode.py.",
    ),
    (
        "track-touched-files.py",
        "Standalone Write|Edit|MultiEdit|NotebookEdit registration folded into postuse-advisory-dispatch.py's hooks.track_touched_files call; fully absent from hooks.json.",
    ),
    (
        "nudge-unauthorized-handoff.py",
        "Its standalone PostToolUse(Write) registration was folded into postuse-advisory-dispatch.py's direct nudge_unauthorized_handoff.advisory_text() call; fully absent from current hooks.json.",
    ),
    (
        "sessionend-auto-commit.py",
        "SessionEnd registration retired 2026-08-27 per PM ruling: commits are EM-initiated. See cross-repo/inbox/2026-08-27-claude-klabauter-em-retire-sessionend-auto-commit.md. Script kept, unregistered.",
    ),
)

#: Tails delivered by more than one path ON PURPOSE, with the count each
#: must show in the exhaustiveness partition. Declared data, never
#: inferred.
#:
#: WANT-COUNT CHANGE FROM THE SOURCE (this port only): the source's `want:
#: 2` for the two `stop_dispatch`-carried rows below counted a CARRIED leg
#: this generator could see via `_stop_family_runner.REAL_STOP_FAMILY_REGISTRY`
#: (a data-carrying registry). `stop_dispatch.py`'s own arrival-note
#: docstring (and `fanin_registries.py`'s own module docstring) records
#: that its eight legs are ordinary Python calls, not a
#: `CARRIED_GUARDS`-declaring registry -- the carried leg still fires at
#: runtime, but is undeclared and therefore invisible to THIS generator's
#: walk (`_load_doe_local_stop_family_registry()` folds an undeclared
#: carrier to `[]`, per that function's own docstring). `want` is
#: therefore 1 for these two today -- what the generator can actually
#: SEE, not what runs -- until a future chunk gives `stop_dispatch.py` a
#: `CARRIED_GUARDS` declaration for these two legs, at which point `want`
#: reverts to 2 and this comment can be deleted.
_DUAL_DELIVERY_REASONS = {
    "scripts/runtime-tripwire-em-check.py": (
        "directly registered on PostToolUse(Agent) and UserPromptSubmit; ALSO fires "
        "at runtime as one of stop_dispatch.py's eight ordinary Python-call legs, but "
        "that leg is undeclared (no CARRIED_GUARDS) and so invisible to this "
        "generator's walk today -- see the want-count note above this dict"
    ),
}
_DUAL_DELIVERY_REASONS["scripts/watchdog-undischarged-next-move.py"] = (
    "TWO EVENTS, ONE SCRIPT, per its own module docstring: directly registered on "
    "PostToolUse(Skill|Agent) to OPEN and DISCHARGE ledger obligations; ALSO carried "
    "by stop_dispatch.py for the one-shot Stop read that surfaces whatever is still "
    "undischarged, but that leg is undeclared (no CARRIED_GUARDS) and so invisible to "
    "this generator's walk today -- see the want-count note above this dict. "
    "main() branches on payload shape. Removing either leg breaks the ledger -- "
    "emission without a read, or a read with nothing recorded."
)
_DUAL_DELIVERY_REASONS["scripts/block-dispatch-suite-invocation.py"] = (
    "TWO EVENTS, ONE SCRIPT: directly registered on PreToolUse(Workflow) (hooks.json's "
    "own _comment on that entry: 'C8 -- Layer 2 of the DR-088 ladder', ordered before "
    "block-workflow-unmodeled-agent.py so its deny wins first), and separately carried "
    "by preuse-agent-dispatch.py's PreToolUse(Agent) fan-in (hooks.json's PRETOOLUSE-AGENT "
    "FAN-IN _comment: folded in as one of the four guards that used to be standalone "
    "Agent-matcher registrations). Workflow and Agent are different tool events -- this "
    "is the same DR-088 suite-invocation deny reused on both, not a leftover duplicate "
    "registration. `preuse-agent-dispatch.py` itself has not landed in this repo yet "
    "(a later chunk, W4-C8) -- its carried leg is invisible to this generator's walk "
    "until that carrier lands, so `want` is 1 today, same reasoning and same reversion "
    "condition as the stop_dispatch-carried pair above."
)
_DECLARED_DUAL_DELIVERY = {
    "scripts/runtime-tripwire-em-check.py": 1,
    "scripts/watchdog-undischarged-next-move.py": 1,
    "scripts/block-dispatch-suite-invocation.py": 1,
}

#: Doctrine-plane guards whose registry `module_key` collides with a
#: DIFFERENT engine-plane guard's name inside the same carrier. Declared
#: data, never a dedup.
_DOCTRINE_LOCAL_ID_OVERRIDES = {
    "check_claude_md_size": "claude_md_admission_gate",
}


class EmitterError(RuntimeError):
    """Any failure that must abort the write closed."""


def tail_key(raw_token: str) -> str:
    """Two-segment, forward-slash-joined, lower-cased tail key -- matches
    the engine plane's own `guard_settings_integrity.py` `_tail_key`'s
    normal form exactly."""
    parts = PureWindowsPath(raw_token).parts
    if len(parts) < 2:
        raise EmitterError(f"token has no two-segment tail key: {raw_token!r}")
    return "/".join(parts[-2:]).lower()


def _check_string(label: str, value: Any) -> str:
    if not isinstance(value, str):
        raise EmitterError(f"{label} is not a string: {value!r}")
    if "\n" in value or "\r" in value:
        raise EmitterError(f"{label} is not single-line: {value!r}")
    if len(value) > MAX_STRING_LEN:
        raise EmitterError(f"{label} exceeds {MAX_STRING_LEN} chars ({len(value)}): {value!r}")
    if not value.isprintable():
        raise EmitterError(f"{label} contains non-printable characters: {value!r}")
    return value


def _matcher_tool_names(matcher: str) -> List[str]:
    return [tok for tok in matcher.split("|") if tok]


def _resolve_doe_content_root() -> Path:
    """DoE plugin content root -- the one remaining cross-plane resolution
    this generator needs (`hooks.json` in, `effective-delivery.json` out).
    Fails closed, mirroring `oss_operative_strings._resolve_mcp_topology_path`'s
    registry-first/pointer-file/content-root seam (W4-C6 precedent for the
    identical displacement class), except this module treats an
    unresolved root as fatal rather than fail-open -- a manifest generator
    has no excuse for a partial or missing result."""
    try:
        from coordinator_core.doe_root_pointer import read_doe_root_pointer
        from coordinator_core.data_root import content_root_for
    except Exception as exc:  # noqa: BLE001
        raise EmitterError(f"cannot import DoE-root resolution seam: {exc}") from exc

    doe_root = read_doe_root_pointer()
    if not doe_root:
        raise EmitterError(
            "DoE root did not resolve (registry/pointer-file rungs all missed) -- "
            "cannot locate hooks.json or the effective-delivery.json sidecar; "
            "aborting closed"
        )
    content_root = content_root_for(doe_root)
    if content_root is None:
        raise EmitterError(
            f"resolved DoE root {doe_root!r} carries neither content layout "
            "(coordinator/ nor a flat published mirror) -- aborting closed"
        )
    return content_root


def _resolve_manifest_paths() -> Tuple[Path, Path]:
    """`(hooks_json_path, manifest_path)`, both under the resolved DoE
    content root. A single resolution call so both paths agree on which
    DoE tree they came from within one `build_block()`/`main()` run."""
    content_root = _resolve_doe_content_root()
    hooks_json_path = content_root.joinpath(*HOOKS_JSON_REL)
    manifest_path = content_root.joinpath(*MANIFEST_REL)
    return hooks_json_path, manifest_path


def _load_hooks_json(hooks_json_path: Path) -> Dict[str, Any]:
    try:
        with hooks_json_path.open("r", encoding="utf-8") as fh:
            return json.load(fh)
    except Exception as exc:  # noqa: BLE001 - fail closed, report why
        raise EmitterError(f"cannot read/parse hooks.json: {exc}") from exc


def _walk_registrations(
    doc: Dict[str, Any],
) -> Tuple[Dict[str, str], Dict[str, Set[str]]]:
    """Structurally partitions every hooks.json `args` array into
    registration tokens vs the bootstrap trampoline. `_comment` prose is
    never scanned -- only actual `args` arrays.

    Returns `(raw_token_by_tail, matchers_by_tail)`."""
    raw_token_by_tail: Dict[str, str] = {}
    matchers_by_tail: Dict[str, Set[str]] = {}

    hooks = doc.get("hooks")
    if not isinstance(hooks, dict):
        raise EmitterError("hooks.json has no top-level 'hooks' object")

    for event_name, entries in hooks.items():
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            matcher = entry.get("matcher", "")
            if not isinstance(matcher, str):
                matcher = ""
            hook_list = entry.get("hooks", [])
            if not isinstance(hook_list, list):
                continue
            for hook in hook_list:
                if not isinstance(hook, dict):
                    continue
                args = hook.get("args", [])
                if not isinstance(args, list):
                    continue
                py_tokens = [a for a in args if isinstance(a, str) and a.endswith(".py")]
                for token in py_tokens:
                    key = tail_key(token)
                    if key in _TRAMPOLINE_TAILS:
                        continue
                    raw_token_by_tail.setdefault(key, token)
                    matchers_by_tail.setdefault(key, set()).add(matcher)

    return raw_token_by_tail, matchers_by_tail


_DoeLocalGuard = namedtuple("_DoeLocalGuard", ["module_key", "filename"])


def _load_doe_local_guard_registry() -> List["_DoeLocalGuard"]:
    """`(module_key, filename)` pairs for `preuse_write_dispatch`'s
    doctrine-plane-local guards -- read from
    `coordinator_core.hooks.fanin_registries.carried_guards()`, keyed by
    the carrier's `coordinator_core.hooks` MODULE name (this package's
    `FANIN_DISPATCHERS` keys), never the DoE-side hyphenated filename the
    source used. A carrier that has landed but declares no
    `CARRIED_GUARDS` (true for `preuse_write_dispatch` today, per that
    module's own arrival-note docstring) correctly contributes zero rows
    here -- caught and folded to `[]`, not propagated as a build failure;
    `fanin_registries.carried_guards()`'s own docstring calls this "not a
    gap" for exactly this carrier."""
    try:
        rows = _fanin_registries.carried_guards("preuse_write_dispatch")
    except AttributeError:
        return []
    return [_DoeLocalGuard(*row) for row in rows]


def _load_engine_write_guard_names() -> List[Tuple[str, str, List[str]]]:
    """Same-repo read of `coordinator_core.write_guards.engine`'s own
    module roster (`discover_guard_names()`) -- no cross-plane import
    needed any more; see module docstring's "WHAT CHANGED -- the
    cross-plane reads". Returns `(guard_name, tail_key, matchers)`
    triples."""
    from coordinator_core.write_guards import engine as write_guards_engine

    discover_guard_names = getattr(write_guards_engine, "discover_guard_names", None)
    if discover_guard_names is None:
        raise EmitterError(
            "coordinator_core.write_guards.engine has no discover_guard_names() "
            "export -- cannot source write guards; aborting closed"
        )
    names, import_failed = discover_guard_names()
    if import_failed:
        raise EmitterError(
            "coordinator_core.write_guards.engine reported import_failed "
            f"guard module(s) {sorted(import_failed)!r} -- an under-counted "
            "manifest would be worse than none; aborting closed"
        )
    pkg_name = getattr(write_guards_engine, "_PKG_NAME", None)
    if not pkg_name:
        raise EmitterError(
            "coordinator_core.write_guards.engine has no _PKG_NAME -- cannot "
            "derive a tail key for its guard modules; aborting closed"
        )
    pkg_tail = pkg_name.split(".")[-1]

    import importlib

    result: List[Tuple[str, str, List[str]]] = []
    for name in names:
        try:
            guard_module = importlib.import_module(f"{pkg_name}.{name}")
        except Exception as exc:  # noqa: BLE001
            raise EmitterError(
                f"cannot re-import {pkg_name}.{name} to read its MATCHERS constant: {exc}"
            ) from exc
        matchers = getattr(guard_module, "MATCHERS", None)
        if not isinstance(matchers, (list, tuple)) or not matchers:
            raise EmitterError(
                f"{pkg_name}.{name} has no usable MATCHERS constant -- held, "
                "not defaulted to the carrier matcher; aborting closed"
            )
        result.append((name, tail_key(f"{pkg_tail}/{name}.py"), list(matchers)))
    return result


def _load_doe_local_stop_family_registry() -> List["_DoeLocalGuard"]:
    """`(module_key, filename)` pairs for `stop_dispatch`'s carried guards
    -- `stop_dispatch` is this repo's landed carrier for DoE's
    `postuse-stop-family-dispatch.py` role (see `FANIN_DISPATCHERS`).
    Same zero-CARRIED_GUARDS-today fold as
    `_load_doe_local_guard_registry`."""
    try:
        rows = _fanin_registries.carried_guards("stop_dispatch")
    except AttributeError:
        return []
    return [_DoeLocalGuard(*row) for row in rows]


#: The advisory carrier's declared op list -- doctrine-plane-derived
#: contract, NOT a transcription of `list_ported_advisory_ops()`'s
#: six-name tuple.
_ADVISORY_OPS_DECLARED: Tuple[str, ...] = (
    "hooks.postuse_advisory_dispatch",
    "hooks.track_touched_files",
)


def _advisory_ops_delivered(hooks_json_path: Path) -> List[str]:
    """Returns `_ADVISORY_OPS_DECLARED`, cross-checked (never sourced)
    against a regex scan of the DoE-resident `postuse-advisory-dispatch.py`
    source, resolved as a sibling of `hooks_json_path`'s own content root
    (`<content root>/hooks/scripts/postuse-advisory-dispatch.py`)."""
    ops = sorted(set(_ADVISORY_OPS_DECLARED))
    if not ops:
        raise EmitterError(
            "_ADVISORY_OPS_DECLARED is empty -- advisory-carrier op list "
            "cannot be derived; aborting closed"
        )

    source_path = hooks_json_path.parent / "scripts" / "postuse-advisory-dispatch.py"
    try:
        text = source_path.read_text(encoding="utf-8")
    except Exception as exc:  # noqa: BLE001
        raise EmitterError(f"cannot read {source_path}: {exc}") from exc

    regex_ops = sorted(
        set(re.findall(r'["\']method["\']\s*:\s*["\'](hooks\.[A-Za-z0-9_]+)["\']', text))
    )
    if regex_ops and regex_ops != ops:
        raise EmitterError(
            f"advisory-carrier op declaration drifted from {source_path}'s own "
            f"dict-literal call sites -- declared {ops!r}, regex cross-check "
            f"found {regex_ops!r}; update _ADVISORY_OPS_DECLARED to match"
        )
    return ops


def build_carrier_write_dispatch(matchers_by_tail: Dict[str, Set[str]]) -> Dict[str, Any]:
    carrier_tail = tail_key(_CARRIER_RAW_TOKENS["write_dispatch"])
    carrier_matcher_tokens = matchers_by_tail.get(carrier_tail, set())
    if len(carrier_matcher_tokens) != 1:
        raise EmitterError(
            f"write_dispatch carrier ({carrier_tail}) matcher is not singular in "
            f"hooks.json: {carrier_matcher_tokens!r}"
        )
    carrier_matcher = next(iter(carrier_matcher_tokens))
    carrier_tool_names = _matcher_tool_names(carrier_matcher)

    registry = _load_doe_local_guard_registry()
    guards = []
    for module_key, filename in registry:
        guard_id = _check_string(
            "write_dispatch guard id",
            _DOCTRINE_LOCAL_ID_OVERRIDES.get(module_key, module_key),
        )
        guard_tail = tail_key(f"hooks/scripts/{filename}")
        guards.append(
            {
                "id": guard_id,
                "script": _check_string("write_dispatch guard script", guard_tail),
                "tool_names": [
                    _check_string("write_dispatch guard tool_names[]", t)
                    for t in carrier_tool_names
                ],
            }
        )

    for name, guard_tail, guard_matchers in _load_engine_write_guard_names():
        guards.append(
            {
                "id": _check_string("write_dispatch guard id", name),
                "script": _check_string("write_dispatch guard script", guard_tail),
                "tool_names": [
                    _check_string("write_dispatch guard tool_names[]", t)
                    for t in guard_matchers
                ],
            }
        )
    return {
        "script": carrier_tail,
        "matcher": _check_string("write_dispatch carrier matcher", carrier_matcher),
        "guards": guards,
    }


def build_carrier_stop_family(matchers_by_tail: Dict[str, Set[str]]) -> Dict[str, Any]:
    carrier_tail = tail_key(_CARRIER_RAW_TOKENS["stop_family"])
    carrier_matcher_tokens = matchers_by_tail.get(carrier_tail, set())
    if len(carrier_matcher_tokens) != 1:
        raise EmitterError(
            f"stop_family carrier ({carrier_tail}) matcher is not singular in "
            f"hooks.json: {carrier_matcher_tokens!r}"
        )
    carrier_matcher = next(iter(carrier_matcher_tokens))
    carrier_tool_names = _matcher_tool_names(carrier_matcher)

    registry = _load_doe_local_stop_family_registry()
    guards = []
    for _module_key, filename in registry:
        guard_tail = tail_key(f"hooks/scripts/{filename}")
        guards.append(
            {
                "id": _check_string("stop_family guard id", filename),
                "script": _check_string("stop_family guard script", guard_tail),
                "tool_names": [
                    _check_string("stop_family guard tool_names[]", t)
                    for t in carrier_tool_names
                ],
            }
        )
    return {
        "script": carrier_tail,
        "matcher": _check_string("stop_family carrier matcher", carrier_matcher),
        "guards": guards,
    }


#: The bash carrier's registered transport after the rehome. An `http`
#: registration has no `args`, so it is structurally invisible to
#: `_walk_registrations` -- resolving it needs its own URL-keyed reader.
_BASH_CARRIER_HTTP_URL = "http://127.0.0.1:47623/hook"


def _http_matchers_for_url(doc: Dict[str, Any], url: str) -> Set[str]:
    out: Set[str] = set()
    hooks = doc.get("hooks")
    if not isinstance(hooks, dict):
        return out
    for _event_name, entries in hooks.items():
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            matcher = entry.get("matcher", "")
            if not isinstance(matcher, str):
                matcher = ""
            hook_list = entry.get("hooks", [])
            if not isinstance(hook_list, list):
                continue
            for hook in hook_list:
                if isinstance(hook, dict) and hook.get("type") == "http" and hook.get("url") == url:
                    out.add(matcher)
    return out


def build_carrier_bash_dispatch(
    matchers_by_tail: Dict[str, Set[str]], doc: Dict[str, Any]
) -> Dict[str, Any]:
    script_tail = tail_key(_CARRIER_RAW_TOKENS["bash_dispatch"])
    command_matchers = matchers_by_tail.get(script_tail, set())
    http_matchers = _http_matchers_for_url(doc, _BASH_CARRIER_HTTP_URL)
    if command_matchers and http_matchers:
        raise EmitterError(
            f"bash_dispatch is registered on BOTH transports -- command "
            f"{sorted(command_matchers)} and http {sorted(http_matchers)}. Two "
            "carriers would deliver the same guard roster; deregister one"
        )
    if http_matchers:
        carrier_tail = "http:" + _BASH_CARRIER_HTTP_URL
        carrier_matcher_tokens = http_matchers
    else:
        carrier_tail = script_tail
        carrier_matcher_tokens = command_matchers
    if len(carrier_matcher_tokens) != 1:
        raise EmitterError(
            f"bash_dispatch carrier ({carrier_tail}) matcher is not singular in "
            f"hooks.json: {carrier_matcher_tokens!r}"
        )
    carrier_matcher = next(iter(carrier_matcher_tokens))
    carrier_matcher_set = set(_matcher_tool_names(carrier_matcher))

    from coordinator_core import bash_guards

    guard_roster = getattr(bash_guards, "guard_roster", None)
    if guard_roster is None:
        raise EmitterError(
            "coordinator_core.bash_guards has no guard_roster() export -- "
            "cannot source guard matchers; aborting closed"
        )
    roster = guard_roster()

    guards = []
    union_tool_names: Set[str] = set()
    for entry in roster:
        matchers = getattr(entry, "matchers", None)
        if not matchers:
            raise EmitterError(
                f"guard_roster() entry {getattr(entry, 'id', '?')!r} has no "
                "readable matchers -- held, not seeded blind; aborting closed"
            )
        guard_id = _check_string("bash_dispatch guard id", entry.id)
        guard_tail = tail_key(entry.script)
        tool_names = [_check_string("bash_dispatch guard tool_names[]", t) for t in matchers]
        union_tool_names.update(tool_names)
        guards.append({"id": guard_id, "script": guard_tail, "tool_names": tool_names})

    if not union_tool_names.issubset(carrier_matcher_set):
        raise EmitterError(
            "bash_dispatch carrier matcher "
            f"{sorted(carrier_matcher_set)} does not cover the union of its "
            f"delivered guards' tool_names {sorted(union_tool_names)} -- a guard "
            "is unreachable under a tool name it declares"
        )

    return {
        "script": carrier_tail,
        "matcher": _check_string("bash_dispatch carrier matcher", carrier_matcher),
        "guards": guards,
    }


def build_carrier_advisory_dispatch(
    matchers_by_tail: Dict[str, Set[str]], hooks_json_path: Path
) -> Dict[str, Any]:
    carrier_tail = tail_key(_CARRIER_RAW_TOKENS["advisory_dispatch"])
    carrier_matcher_tokens = matchers_by_tail.get(carrier_tail, set())
    if len(carrier_matcher_tokens) != 1:
        raise EmitterError(
            f"advisory_dispatch carrier ({carrier_tail}) matcher is not singular "
            f"in hooks.json: {carrier_matcher_tokens!r}"
        )
    carrier_matcher = next(iter(carrier_matcher_tokens))

    ops = [_check_string("advisory_dispatch op id", op) for op in _advisory_ops_delivered(hooks_json_path)]

    return {
        "script": carrier_tail,
        "matcher": _check_string("advisory_dispatch carrier matcher", carrier_matcher),
        "guards": [],
        "ops": ops,
    }


def build_carrier_fanin(
    dispatcher_filename: str, matchers_by_tail: Dict[str, Set[str]]
) -> Dict[str, Any]:
    """Carrier entry for one SessionStart/Stop fan-in dispatcher. The guard
    list is read LIVE from the dispatcher's own carried-guards convention
    via `coordinator_core.hooks.fanin_registries`, never transcribed.

    Unlike the tool-event carriers, a fan-in dispatcher's matcher is a
    SOURCE set, not a tool set, so `tool_names` is empty."""
    carrier_tail = tail_key("${CLAUDE_PLUGIN_ROOT}/hooks/scripts/" + dispatcher_filename)
    carrier_matcher_tokens = matchers_by_tail.get(carrier_tail, set())
    if len(carrier_matcher_tokens) != 1:
        raise EmitterError(
            f"fan-in carrier ({carrier_tail}) matcher is not singular in "
            f"hooks.json: {carrier_matcher_tokens!r}"
        )
    carrier_matcher = next(iter(carrier_matcher_tokens))

    guards = []
    for module_key, guard_filename in _fanin_registries.carried_guards(dispatcher_filename):
        guards.append(
            {
                "id": _check_string("fan-in guard id", module_key),
                "script": _check_string("fan-in guard script", "scripts/" + guard_filename),
                "tool_names": [],
            }
        )
    return {
        "script": carrier_tail,
        "matcher": _check_string("fan-in carrier matcher", carrier_matcher),
        "guards": guards,
    }


def build_direct_entries(
    raw_token_by_tail: Dict[str, str],
    matchers_by_tail: Dict[str, Set[str]],
    carrier_tails: Set[str],
) -> List[Dict[str, Any]]:
    direct = []
    for tail, _raw in sorted(raw_token_by_tail.items()):
        if tail in carrier_tails:
            continue
        tool_names = sorted(
            {t for m in matchers_by_tail.get(tail, set()) for t in _matcher_tool_names(m)}
        )
        direct.append(
            {
                "id": _check_string("direct entry id", tail.split("/")[-1]),
                "script": _check_string("direct entry script", tail),
                "tool_names": [_check_string("direct entry tool_names[]", t) for t in tool_names],
            }
        )
    return direct


def build_retired_entries() -> List[Dict[str, Any]]:
    retired = []
    for filename, reason in _RETIRED:
        tail = tail_key(f"hooks/scripts/{filename}")
        retired.append(
            {
                "id": _check_string("retired entry id", filename),
                "script": _check_string("retired entry script", tail),
                "reason": _check_string("retired entry reason", reason),
            }
        )
    return retired


def _run_git(*args: str, cwd: Any = None) -> str:
    """Runs `git <args>` with an explicit timeout and never `shell=True`,
    in `REPO_ROOT` unless `cwd` names another tree (the resolved DoE
    content root's repo, for the `hooks.json` half of the provenance)."""
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=REPO_ROOT if cwd is None else cwd,
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT_SECONDS,
            shell=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise EmitterError(f"cannot run 'git {' '.join(args)}': {exc}") from exc
    if result.returncode != 0:
        raise EmitterError(
            f"'git {' '.join(args)}' exited {result.returncode}: {result.stderr.strip()}"
        )
    return result.stdout


@lru_cache(maxsize=1)
def _doe_source_provenance(doe_repo_root: Path) -> Dict[str, Any]:
    """The DoE half of `PROVENANCE_KEYS`: which DoE tree revision
    `hooks.json` was actually read from -- see module docstring's "WHAT
    CHANGED -- emission provenance". Fails closed like every other
    cross-plane read here."""
    sha = _run_git("rev-parse", "HEAD", cwd=doe_repo_root).strip()
    if len(sha) != 40 or not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise EmitterError(
            f"'git rev-parse HEAD' in DoE root did not return a 40-char hex SHA: {sha!r}"
        )
    status = _run_git("status", "--porcelain", "--untracked-files=no", cwd=doe_repo_root)
    return {
        "doe_source_sha": sha,
        "doe_source_dirty_tree": bool(status.strip()),
    }


def _emission_provenance(hooks_json_path: Path) -> Dict[str, Any]:
    """Resolves `PROVENANCE_KEYS` at emission time: this repo's full HEAD
    SHA, a UTC ISO-8601 second-precision `Z`-suffixed timestamp, whether
    this repo's tracked working tree is dirty, and the DoE half from
    `_doe_source_provenance()`."""
    sha = _run_git("rev-parse", "HEAD").strip()
    if len(sha) != 40 or not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise EmitterError(f"'git rev-parse HEAD' did not return a 40-char hex SHA: {sha!r}")

    status = _run_git("status", "--porcelain", "--untracked-files=no")
    dirty = bool(status.strip())

    generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    # DoE repo root is two levels above the resolved content root under the
    # private-authoring layout (<doe_root>/coordinator/) and the content
    # root itself under the flat published-mirror layout (its own .git).
    doe_content_root = hooks_json_path.parents[2]
    doe_repo_root = doe_content_root if (doe_content_root / ".git").exists() else doe_content_root.parent

    return {
        "generated_from_sha": sha,
        "generated_at": generated_at,
        "generated_from_dirty_tree": dirty,
        **_doe_source_provenance(doe_repo_root),
    }


def build_block() -> Dict[str, Any]:
    """The manifest block for the current `hooks.json`, built and validated
    against one read of it."""
    hooks_json_path, _manifest_path = _resolve_manifest_paths()
    doc = _load_hooks_json(hooks_json_path)
    raw_token_by_tail, matchers_by_tail = _walk_registrations(doc)

    carrier_write_dispatch = build_carrier_write_dispatch(matchers_by_tail)
    carrier_stop_family = build_carrier_stop_family(matchers_by_tail)
    carrier_bash_dispatch = build_carrier_bash_dispatch(matchers_by_tail, doc)
    carrier_advisory_dispatch = build_carrier_advisory_dispatch(matchers_by_tail, hooks_json_path)

    carrier_list = [
        carrier_write_dispatch,
        carrier_stop_family,
        carrier_bash_dispatch,
        carrier_advisory_dispatch,
    ] + [
        build_carrier_fanin(dispatcher, matchers_by_tail)
        for dispatcher in _fanin_registries.FANIN_DISPATCHERS
        if dispatcher not in _DEDICATED_BUILDER_FANIN_FILENAMES
    ]
    carriers: Dict[str, Dict[str, Any]] = {}
    for c in carrier_list:
        if c["script"] in carriers:
            raise EmitterError(
                f"carrier tail {c['script']!r} built twice -- two carrier "
                "builders produced entries for the same tail key. Add the "
                "later one's dispatcher filename to "
                "_DEDICATED_BUILDER_FANIN_FILENAMES if it has a dedicated "
                "builder, or fix the duplicate registration."
            )
        carriers[c["script"]] = c
    carrier_tails = set(carriers)

    direct = build_direct_entries(raw_token_by_tail, matchers_by_tail, carrier_tails)
    retired = build_retired_entries()

    seen_tails: Dict[str, int] = {}
    for carrier in carriers.values():
        for guard in carrier["guards"]:
            seen_tails[guard["script"]] = seen_tails.get(guard["script"], 0) + 1
    for entry in direct:
        seen_tails[entry["script"]] = seen_tails.get(entry["script"], 0) + 1

    for tail in raw_token_by_tail:
        count = seen_tails.get(tail, 0)
        if tail in carrier_tails:
            continue
        want = _DECLARED_DUAL_DELIVERY.get(tail, 1)
        if count != want:
            detail = (
                f" -- declared dual delivery ({_DUAL_DELIVERY_REASONS[tail]})"
                if tail in _DECLARED_DUAL_DELIVERY
                else ""
            )
            raise EmitterError(
                f"exhaustiveness violation: tail {tail!r} appears in "
                f"{count} of carriers[*].guards[*]/direct[*] (want exactly "
                f"{want}){detail}. A handler both registered directly and carried "
                "by a fan-in dispatcher runs twice per event; if that is deliberate, "
                "declare it in _DECLARED_DUAL_DELIVERY with its reason."
            )

    for surface_name, entries in [
        *((f"carrier {k}", c["guards"]) for k, c in carriers.items()),
        ("direct", direct),
    ]:
        seen_ids: Dict[str, int] = {}
        for entry in entries:
            seen_ids[entry["id"]] = seen_ids.get(entry["id"], 0) + 1
        repeated = sorted(i for i, n in seen_ids.items() if n > 1)
        if repeated:
            raise EmitterError(
                f"duplicate guard id(s) {repeated} within {surface_name} -- that surface "
                "declares the same id twice. If these are two different guards whose names "
                "collide, declare the doctrine-plane one in _DOCTRINE_LOCAL_ID_OVERRIDES; "
                "do not dedupe them."
            )

    retired_tails = {r["script"] for r in retired}
    overlap = retired_tails & set(raw_token_by_tail)
    if overlap:
        raise EmitterError(
            f"exhaustiveness violation: retired tail(s) {sorted(overlap)} "
            "are live registrations in hooks.json -- retired list is stale"
        )

    block = {
        "version": 1,
        **_emission_provenance(hooks_json_path),
        "carriers": carriers,
        "direct": direct,
        "retired": retired,
    }
    return block


def render_block(block: Dict[str, Any]) -> str:
    """Deterministic serialization: stable key order, no timestamps in the
    delivery-graph body -- the provenance keys are deliberately exempt.
    This is also the exact on-disk form of the manifest path (plus a
    trailing newline)."""
    return json.dumps({MANIFEST_KEY: block}, indent=2, sort_keys=False, ensure_ascii=False)


def write_block(block: Dict[str, Any], manifest_path: Path) -> None:
    """Replaces `manifest_path` atomically. `hooks.json` is only ever read
    by this module, never written."""
    tmp_path = manifest_path.with_suffix(".json.tmp")
    with tmp_path.open("w", encoding="utf-8", newline="\n") as fh:
        fh.write(render_block(block))
        fh.write("\n")
    tmp_path.replace(manifest_path)


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--write",
        action="store_true",
        help="Write the block to the resolved DoE content root's "
        "hooks/effective-delivery.json (default: print mode, mutates nothing).",
    )
    args = parser.parse_args(argv)

    try:
        _hooks_json_path, manifest_path = _resolve_manifest_paths()
        block = build_block()
    except EmitterError as exc:
        print(f"emit_effective_delivery: FAILED CLOSED: {exc}", file=sys.stderr)
        return 1

    if args.write:
        write_block(block, manifest_path)
        print(f"emit_effective_delivery: wrote block to {manifest_path}", file=sys.stderr)
        return 0

    print(render_block(block))
    return 0


if __name__ == "__main__":
    sys.exit(main())
