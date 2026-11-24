"""Checkpoint own-hunk staging: the blobs that carry only the checkpointing run's hunks of a path.

A checkpoint commit names its plan in a `Checkpoint-Plan:` line. `checkpoint_run_id` resolves the
live run of the committing session that works that plan; `checkpoint_own_blobs` then splits each
listed path with the same attribution `land_run` uses (`run_hunk_claims.own_hunks`) and returns
`supplied_blobs` for `commit_paths`. Zero git spawns; every unknown withholds, nothing raises.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Optional, Sequence


_RUN_ID_TRAILER = re.compile(r"^Checkpoint-Run-Id:([^\r\n]*)", re.M)


def _norm(path: str) -> str:
    return str(path).replace("\\", "/")


def _sizing_plan(root: Path, sizing_rel: object) -> str:
    import yaml

    if not isinstance(sizing_rel, str) or not sizing_rel:
        return ""
    try:
        sizing = yaml.safe_load((root / sizing_rel).read_text(encoding="utf-8")) or {}
    except (OSError, ValueError, yaml.YAMLError):
        return ""
    plan = sizing.get("plan") if isinstance(sizing, dict) else None
    return _norm(plan) if isinstance(plan, str) else ""


def _receipt_plan(root: Path, script_path: object) -> str:
    """The plan a plan-route emit recorded in `<script>.emitted.json` (`plan_relpath`), "" when the
    script has no readable receipt or it names no plan. One small file read, no spawn."""
    import json

    if not isinstance(script_path, str) or not script_path:
        return ""
    try:
        receipt = json.loads(Path(f"{root / script_path}.emitted.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ""
    plan = receipt.get("plan_relpath") if isinstance(receipt, dict) else None
    return _norm(plan) if isinstance(plan, str) else ""


def _record_plan(root: Path, record: dict) -> str:
    """The plan a live run works: its bound sizing's plan, else (a plan re-emit binds no sizing) the
    plan its emission receipt names."""
    from coordinator_core.ops import warp_run_deliverable

    sizing_rel = record.get("sizing_rel") or warp_run_deliverable.record_sizing_rel(root, record["run_id"])
    return _sizing_plan(root, sizing_rel) or _receipt_plan(root, record.get("script_path"))


def _message_plan(message: str) -> str:
    from coordinator_core.git.checkpoint_guard import _PLAN_RE

    m = _PLAN_RE.search(message or "")
    return m.group("v") if m else ""


def checkpoint_run_id(root: Path, message: str, session_id: Optional[str]) -> Optional[str]:
    """The run id of the one live run of `session_id` whose sizing's plan is the message's
    `Checkpoint-Plan:`; None when the message names no plan, the session is unknown, or zero or
    several runs match. A token record that does not name its sizing (the pre-answer marker, or a
    bind that did not rewrite it) falls back to the sizing its record-dir `run.json` names."""
    try:
        if not session_id:
            return None
        plan = _message_plan(message)
        if not plan:
            return None
        requested = [value.strip() for value in _RUN_ID_TRAILER.findall(message)]
        if len(requested) > 1 or requested == [""]:
            return None
        from coordinator_core.git.checkpoint_guard import checkpoint_plan_matches
        from coordinator_core.ops.dispatch_emit import run_token

        root = Path(root)
        found = [
            record["run_id"]
            for record in run_token.live_records(root)
            if record.get("session_id") == session_id
            and (not requested or record["run_id"] == requested[0])
            and (own := _record_plan(root, record))
            and checkpoint_plan_matches(plan, own)
        ]
        return found[0] if len(found) == 1 else None
    except Exception:  # noqa: BLE001 -- unresolvable: the caller withholds
        return None


def _is_test_path(path: str) -> bool:
    parts = _norm(path).split("/")
    name = parts[-1]
    return (
        any(p in ("tests", "test", "__tests__") for p in parts[:-1])
        or name.startswith("test_")
        or name.endswith(("_test.py", ".test.ts", ".test.tsx", ".test.js", ".spec.ts", ".spec.js"))
    )


def tests_of_withheld_code(
    root: Path, message: str, paths: Sequence[str], withheld: Sequence[str],
) -> list[str]:
    """The `paths` that are test files of a checkpoint row whose non-test write is in `withheld`:
    a row lands whole or its tests wait with its withheld code. Rows come from the message subject,
    their `writes` from the `Checkpoint-Plan:` spine. Zero git spawns; an unreadable plan returns
    `[]`. Never raises."""
    try:
        from coordinator_core.git.checkpoint_guard import checkpoint_row_ids
        from coordinator_core.ops.dispatch_emit.spine_read import load_rows_memo

        plan, ids = _message_plan(message), set(checkpoint_row_ids((message or "").split("\n", 1)[0]))
        if not plan or not ids or not withheld:
            return []
        rows = getattr(load_rows_memo((Path(root) / plan).read_text(encoding="utf-8")), "rows", None) or []
        held, present = {_norm(p) for p in withheld}, {_norm(p) for p in paths}
        extra: set[str] = set()
        for row in rows:
            writes = row.get("writes") if isinstance(row, dict) else None
            if str(row.get("id") if isinstance(row, dict) else "") not in ids or not isinstance(writes, list):
                continue
            norm = [_norm(w) for w in writes if isinstance(w, str)]
            if any(w in held and not _is_test_path(w) for w in norm):
                extra.update(w for w in norm if _is_test_path(w) and w in present and w not in held)
        return sorted(extra)
    except Exception:  # noqa: BLE001 -- unreadable plan: nothing extra is withheld
        return []


def _names_withheld(source: str, held_modules: dict[str, str], held_paths: set[str]) -> Optional[str]:
    """The withheld path `source` imports (`import m`, `from m import x`, `from parent import leaf`,
    absolute only) or names as a string literal (the path, or a dotted `module[.attr]`), else None."""
    import ast

    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        return None
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            for mod in (node.module, *(f"{node.module}.{a.name}" for a in node.names)):
                if mod in held_modules:
                    return f"imports {mod}, withheld {held_modules[mod]}"
        elif isinstance(node, ast.Import):
            for a in node.names:
                if a.name in held_modules:
                    return f"imports {a.name}, withheld {held_modules[a.name]}"
        elif isinstance(node, ast.Constant) and isinstance(node.value, str) and len(node.value) < 300:
            lit = _norm(node.value)
            if lit in held_paths:
                return f"names {lit}, withheld"
            mod = lit
            while mod:
                if mod in held_modules:
                    return f"names {lit}, withheld {held_modules[mod]}"
                mod, _, _ = mod.rpartition(".")
    return None


def tests_exercising_withheld_code(
    root: Path, paths: Sequence[str], withheld: Sequence[str], blobs: Optional[dict] = None,
) -> dict[str, str]:
    """`{path: why}` for each test file in `paths` (staged or untracked) that imports a `.py` in
    `withheld` or names a withheld path as a string literal. A withheld test is itself a withheld
    path for the next pass, so the result is a fixpoint. A path's text is its `blobs` entry when
    supplied, else the worktree file. In-process ast read, zero git spawns; never raises, and an
    unreadable or unparsable test judges nothing."""
    try:
        from coordinator_core.git.git_dir import resolve_git_common_dir
        from coordinator_core.git.git_objects import read_object

        root = Path(root)
        blobs = {_norm(k): v for k, v in (blobs or {}).items()}
        held = {_norm(p) for p in withheld}
        tests = [_norm(p) for p in paths if _is_test_path(p) and _norm(p) not in held]
        if not held or not tests:
            return {}
        common = resolve_git_common_dir(root)

        def text(path: str) -> Optional[str]:
            try:
                if path in blobs:
                    got = read_object(common, blobs[path])
                    return got[1].decode("utf-8", "replace") if got else None
                return (root / path).read_text(encoding="utf-8", errors="replace")
            except OSError:
                return None

        def module_of(path: str) -> str:
            mod = path[:-3].replace("/", ".")
            return mod[: -len(".__init__")] if mod.endswith(".__init__") else mod

        sources = {p: text(p) for p in tests if p.endswith(".py")}
        found: dict[str, str] = {}
        grew = True
        while grew:
            grew = False
            modules = {module_of(p): p for p in held if p.endswith(".py")}
            for path, src in sources.items():
                if path in found or not src:
                    continue
                why = _names_withheld(src, modules, held)
                if why:
                    found[path] = why
                    held.add(path)
                    grew = True
        return found
    except Exception:  # noqa: BLE001 -- unjudgeable: nothing extra is withheld
        return {}


def _definitions(source: Optional[str]) -> tuple[set[str], dict[str, set[str]]]:
    """`(top-level def/class names, {function name: parameter names})` of `source`; empty when it
    is absent or does not parse."""
    import ast

    if not source:
        return set(), {}
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        return set(), {}
    names: set[str] = set()
    params: dict[str, set[str]] = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            a = node.args
            params.setdefault(node.name, set()).update(
                x.arg for x in [*a.posonlyargs, *a.args, *a.kwonlyargs]
            )
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            names.update(t.id for t in node.targets if isinstance(t, ast.Name))
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
    return names, params


def _new_definitions(live: Optional[str], head: Optional[str]) -> tuple[set[str], dict[str, set[str]]]:
    """Names and per-function parameters present in `live` and absent from `head`."""
    live_names, live_params = _definitions(live)
    head_names, head_params = _definitions(head)
    new_params = {
        fn: ps - head_params.get(fn, set()) for fn, ps in live_params.items()
        if ps - head_params.get(fn, set())
    }
    return live_names - head_names, new_params


def uses_new_definition(
    source: str, module: str, new_names: set[str], new_params: dict[str, set[str]],
    package: Optional[str] = None, star: bool = False,
) -> Optional[str]:
    """What `source` uses from `module`'s new definitions, or None. A consumer must import `module`
    (dotted) by `import m`, `from m import x` or `from parent import leaf`. A relative import
    (`from . import leaf`, `from .leaf import x`) resolves against `package`, the dotted package of
    the consumer; with `package=None` it is not resolved, so such a consumer is not held. An
    attribute or keyword match is by name alone, so an unrelated same-named attribute over-holds
    (the safe side). `star=True` also judges a bare name use in a `from module import *` consumer."""
    import ast

    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        return None
    parent, _, leaf = module.rpartition(".")
    pkg_parts = package.split(".") if package else []
    imported = False
    starred = False
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            from_module = node.module
            if node.level:
                keep = len(pkg_parts) - (node.level - 1)
                if package is None or keep < 0 or (keep == 0 and not node.module):
                    continue
                from_module = ".".join([*pkg_parts[:keep], *([node.module] if node.module else [])])
            if from_module == module:
                imported = True
                starred = starred or any(a.name == "*" for a in node.names)
                for alias in node.names:
                    if alias.name in new_names:
                        return f"imports {module}.{alias.name}"
            elif from_module == parent and any(a.name == leaf for a in node.names):
                imported = True
        elif isinstance(node, ast.Import) and any(a.name == module for a in node.names):
            imported = True
    if not imported:
        return None
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr in new_names:
            return f"uses {module}.{node.attr}"
        if star and starred and isinstance(node, ast.Name) and node.id in new_names:
            return f"uses {module}.{node.id} via import *"
        if isinstance(node, ast.Call):
            fn = node.func
            callee = fn.id if isinstance(fn, ast.Name) else fn.attr if isinstance(fn, ast.Attribute) else None
            fresh = new_params.get(callee or "", set())
            for kw in node.keywords:
                if kw.arg and kw.arg in fresh:
                    return f"calls {callee}(..., {kw.arg}=...)"
    return None


def consumers_of_withheld_definitions(
    root: Path, paths: Sequence[str], withheld: Sequence[str], blobs: Optional[dict] = None,
) -> dict[str, str]:
    """`{path: why}` for each staged `.py` in `paths` that imports a withheld `.py` and uses a
    name or keyword that exists only in that withheld path's worktree content (absent from HEAD).
    A withheld consumer is itself a producer for the next pass, so the result is a fixpoint. Reads
    objects in-process, zero git spawns; never raises, and an unreadable path judges nothing."""
    try:
        from coordinator_core.git.git_objects import read_object
        from coordinator_core.git.git_dir import resolve_git_common_dir
        from coordinator_core.git.git_state import head_blobs

        root = Path(root)
        common = resolve_git_common_dir(root)
        blobs = {_norm(k): v for k, v in (blobs or {}).items()}
        held = {_norm(p) for p in withheld if _norm(p).endswith(".py")}
        staged = [_norm(p) for p in paths if _norm(p).endswith(".py") and _norm(p) not in held]
        if not held or not staged:
            return {}

        def text(blob: Optional[str]) -> Optional[str]:
            got = read_object(common, blob) if blob else None
            return got[1].decode("utf-8", "replace") if got else None

        heads = head_blobs(root, sorted(held | set(staged)))

        def live(path: str) -> Optional[str]:
            try:
                return (root / path).read_text(encoding="utf-8", errors="replace")
            except OSError:
                return None

        def producer(path: str) -> tuple[set[str], dict[str, set[str]]]:
            return _new_definitions(live(path), text(heads[path][1]) if path in heads else None)

        sources = {
            p: (text(blobs[p]) if p in blobs else live(p)) for p in staged
        }
        found: dict[str, str] = {}
        grew = True
        while grew:
            grew = False
            for prod in sorted(held):
                names, params = producer(prod)
                if not names and not params:
                    continue
                module = prod[:-3].replace("/", ".")
                if module.endswith(".__init__"):
                    module = module[: -len(".__init__")]
                for path in staged:
                    src = sources.get(path)
                    if path in found or not src:
                        continue
                    why = uses_new_definition(src, module, names, params)
                    if why:
                        found[path] = f"{why}, defined only in withheld {prod}"
                        held.add(path)
                        grew = True
        return found
    except Exception:  # noqa: BLE001 -- unjudgeable: nothing extra is withheld
        return {}


NO_PRINT_REASON = "no write-time print of this run: the run's own write of this path was not recorded"


def _hunk_label(hunk: object) -> str:
    return f"@@ -{hunk.old_start},{hunk.old_len} @@"  # type: ignore[attr-defined]


def _nonrun_reason(nonrun: Sequence[object]) -> str:
    labels = [f"{_hunk_label(h)} (line {h.old_start})" for h in nonrun]  # type: ignore[attr-defined]
    return "hunk a non-run edit wrote after this run's last recorded write: " + ", ".join(labels)


def _withheld_reason(split: object, hunks: Sequence[object]) -> str:
    overlapping = getattr(split, "overlapping", None) or {}
    unattributed = getattr(split, "unattributed", None) or []
    if overlapping:
        held = [f"{h} of run {run}" for run, hs in overlapping.items() for h in map(_hunk_label, hs)]
        return "hunk overlaps a peer run's: " + ", ".join(held)
    if unattributed:
        return "hunk no run baseline attributes to this run: " + ", ".join(map(_hunk_label, unattributed))
    nonrun = getattr(split, "nonrun", None) or []
    if nonrun:
        return _nonrun_reason(nonrun)
    return "no hunk is this run's: " + ", ".join(map(_hunk_label, hunks))


HEAD_EQUAL_REASON = "the run's own hunks reduce this path to HEAD's bytes while the worktree differs: its other hunks are not this run's"


def withhold_head_equal(
    root: Path, blobs: dict[str, str], reasons: Optional[dict[str, str]] = None,
) -> tuple[dict[str, str], list[str]]:
    """`(blobs, withheld)`: `blobs` without each path whose supplied blob is HEAD's own blob (it
    would reach the per-path delta pass as "already match HEAD" while the worktree differs), and
    those paths, each named in `reasons`. Never raises; an unreadable HEAD tree withholds nothing."""
    try:
        from coordinator_core.git.git_state import head_blobs

        head = head_blobs(Path(root), list(blobs))
        same = sorted(p for p, sha in blobs.items() if p in head and head[p][1] == sha)
    except Exception:  # noqa: BLE001 -- unreadable HEAD: the delta pass judges
        return dict(blobs), []
    for path in same:
        if reasons is not None:
            reasons[path] = HEAD_EQUAL_REASON
    return {p: s for p, s in blobs.items() if p not in same}, same


def _apply_peer_recorded(
    root: Path, run_id: str, paths: Sequence[str], blobs: dict[str, str], withheld: list[str],
    why: dict[str, str],
) -> None:
    """For each `paths` entry the write-print split calls wholly the run's own, `checkpoint_candidates`
    judges the same bytes by what runs recorded: a hunk a named peer run recorded is left out of the
    path's blob (it stays dirty in the worktree), and a path whose every hunk lacks the run's record
    while a peer's is named is withheld naming lines and owner. A hunk owned by `unknown` changes
    nothing: write-print attribution stands. Never raises; an unwritable blob withholds."""
    if not paths:
        return
    try:
        from coordinator_core.git.git_dir import resolve_git_common_dir
        from coordinator_core.git.git_objects import write_object
        from coordinator_core.ops.dispatch_emit.own_hunk_candidate import UNKNOWN_OWNER
        from coordinator_core.ops.dispatch_emit.terminal_commit_checkpoints import checkpoint_candidates
    except ImportError:  # candidate reader not landed with this module: write-print attribution stands
        return

    def named(entries: object) -> list[dict]:
        return [e for e in entries or [] if isinstance(e, dict) and e.get("owner") not in (None, UNKNOWN_OWNER)]

    for path, res in checkpoint_candidates(root, run_id, paths).items():
        if "refused" in res:
            peers = named(res.get("foreign"))
            if peers:
                withheld.append(path)
                why[path] = "hunk a peer run recorded: " + ", ".join(
                    f"lines {e['lines']} of run {e['owner']}" for e in peers
                )
        elif named(res.get("excluded")):
            try:
                blobs[path] = write_object(resolve_git_common_dir(root), b"blob", res["bytes"])
            except OSError:
                withheld.append(path)
                why[path] = "candidate blob unwritable"


def checkpoint_own_blobs(
    root: Path, run_id: Optional[str], paths: Sequence[str], message: str = "",
    reasons: Optional[dict[str, str]] = None,
) -> tuple[dict[str, str], list[str]]:
    """`(supplied_blobs, withheld)`: `{path: blob sha}` for each tracked path whose worktree carries
    foreign hunks beside the run's own (the blob is HEAD plus the own hunks only), and the paths
    held whole off the commit. A path with no diff or absent from HEAD appears in neither; a path
    wholly the run's own by write-print appears in neither unless `checkpoint_candidates` names a
    peer-recorded hunk in it (blob without that hunk, or withheld). A diffed path the run has no write-time print for (`run_write_prints`) is
    withheld whole and named: its hunks cannot be told from a peer's, unless a chunk credited for the
    run declares the path (an earlier pass wrote it; claims alone attribute it). `message` supplies the
    `Checkpoint-Plan:` value that scopes the peer-hold read. A `reasons` dict, when given, receives
    `{path: why}` for each withheld path, naming the foreign hunk where one is known. Never raises;
    unknown (no run, unreadable diff, unattributed or overlapping hunk) withholds."""
    root = Path(root)
    why = reasons if reasons is not None else {}
    wanted = [_norm(p) for p in paths]
    blobs: dict[str, str] = {}
    withheld: list[str] = []
    try:
        from coordinator_core.ops.dispatch_emit import (
            family_credit, own_hunk_stage, peer_hunk_gate, run_credit, run_hunk_claims, run_write_prints,
        )

        try:
            diffs = own_hunk_stage.read_diff(root, wanted)
        except own_hunk_stage.OwnHunkRefused:
            why.update({p: "diff unreadable" for p in wanted})
            return {}, sorted(set(wanted))
        pending = [p for p in wanted if p in diffs and diffs[p].hunks]
        if not pending:
            return {}, []
        if not run_id:
            why.update({p: "run unresolved: no live run of this session works the Checkpoint-Plan" for p in pending})
            return {}, sorted(set(pending))
        prints = run_write_prints.read(root, run_id) or {}
        unprinted = [p for p in dict.fromkeys(pending) if p not in prints]
        credited = run_credit.credited_writes(root, run_id) if unprinted else frozenset()
        uncredited = [p for p in unprinted if p not in credited]
        family, family_why = family_credit.credit(root, run_id, uncredited, message) if uncredited else (frozenset(), {})
        credited = credited | family
        for path in [p for p in uncredited if p not in family]:
            withheld.append(path)
            why[path] = (
                (
                    family_why[path]
                    if family_why[path].startswith(family_credit.AUTHORED_FALSE)
                    else f"family-stage failure: {family_why[path]}"
                )
                if path in family_why
                else NO_PRINT_REASON
            )
        pending = [p for p in pending if p in prints or p in credited]
        if not pending:
            return {}, sorted(set(withheld))
        pairs = peer_hunk_gate.co_declared_peer_holds(
            root, run_id, pending, pending, own_plan=_message_plan(message) or None,
        )
        holders: dict[str, dict[str, bool]] = {}
        for path, sibling in pairs:
            holders.setdefault(_norm(path), {})[sibling] = True
        whole: list[str] = []
        for path in dict.fromkeys(pending):
            diff = diffs[path]
            hunks = list(diff.hunks)
            split = run_hunk_claims.own_hunks(root, run_id, path, hunks, holders.get(path, {}))
            if split.overlapping or split.unattributed or not split.own:
                withheld.append(path)
                why[path] = _withheld_reason(split, hunks)
            elif len(split.own) != len(hunks):
                if split.nonrun:
                    why[path] = _nonrun_reason(split.nonrun)
                blobs[path] = own_hunk_stage.own_blob(root, path, diff.head_bytes, split.own)
            else:
                whole.append(path)
        _apply_peer_recorded(root, run_id, whole, blobs, withheld, why)
    except Exception:  # noqa: BLE001 -- unknown holds
        why.update({p: "attribution read failed" for p in wanted if p not in why})
        return {}, sorted(set(wanted))
    return blobs, sorted(set(withheld))
