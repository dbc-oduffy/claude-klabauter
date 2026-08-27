"""coordinator_core.ceremony_common.test_argv_prog_slot_contract — a static,
manifest-driven guard against the "prog-slot" argv mismatch class of defect
in the `workday_complete`/`workweek_complete`/`workstream_complete` apply
dispatchers.

Background: each of the three ceremony `apply.py` modules' `_invoke_cli_main`
dispatches a directive's CLI `main(argv)` with `main_fn(list(args))` —
`args` is args-only, with NO `argv[0]` program-name placeholder spliced in
(that splice only happens on the OTHER branch, for a zero-arg `main()`
trampoline that reads `sys.argv` itself). The established, correct
convention for an argv-TAKING `main(argv)` is therefore also args-only —
confirmed by `workday-complete-reconcile.py`'s own docstring comment (fixed
2026-07-26) and empirically by the overwhelming majority of consumes-
manifest CLIs, which do `parser.parse_args(argv)` or treat `argv[0]` itself
as a real token (a subcommand, a repo-root override, etc.) — NEVER
`argv[1:]` to skip a program-name slot that was never spliced in.

Three CLIs violated this convention (2026-07-27 arg-mismatch audit):
`coordinator/bin/wsc-tail.py`, `coordinator/bin/reconcile-completion-
commits.py`, and `coordinator/bin/workweek-complete-close.py` all did
`argv[1:]` (directly or via `parser.parse_args(argv[1:])`), silently
discarding the first real argument (e.g. `--sid`) whenever apply dispatched
them in-process — the reason `d-run-wsc-tail` had never once succeeded
through `/workstream-complete apply`. All three are fixed as of this audit;
this file is the guard against a repeat, or a newly-added CLI landing on
the wrong side of the same contract.

This test is DELIBERATELY static (AST/regex over the CLI's own source),
not an execution-level test — the manifest spans ~60 heterogeneous CLIs
(argparse-based, hand-rolled subcommand routers, zero-arg trampolines) with
wildly different argument grammars and side effects (git mutation, file
writes, subprocess dispatch); a single synthetic "canary" argv could not
discriminate the bug across all of them without false positives (an
argparse CLI with a REQUIRED flag masks the "unrecognized argument" signal
before it ever fires — verified empirically against `wsc-tail.py` itself
during this audit). Reading the CLI's own `main()` body for the exact
textual shape of the bug is the reliable, cheap check; execution-level
proof for the three fixed CLIs lives in their own test suites
(`coordinator/tests/test_wsc_tail_trampoline.py`,
`coordinator/tests/test_reconcile_completion_commits.py`,
`coordinator/bin/tests/test_workweek_complete_close.py`), which already
exercise the real module's `main()` with real flags.

Driven from the manifest (`CONSUMES_MANIFEST` — the single oracle each
apply.py dispatches over), not a hand-typed subset: a newly-added CLI on
the wrong contract fails here by name, with no separate registration step.

Manifest union — auto-discovered, not hand-imported (2026-07-27
generalization). This file originally imported exactly three named
`CONSUMES_MANIFEST` constants (`workday_complete`, `workstream_complete`,
`workweek_complete`) by hand. `phantom_resolves_sweep.
discover_consumes_manifest_modules` proves those three are, in fact, the
ENTIRE set of `coordinator_core` packages that define a module-level
`CONSUMES_MANIFEST` today — every other `brief(`-defining package
(`backlog_grind_assemble`, `baton_assemble`, `consolidate_assemble`,
`merge_assemble`, `orient_assemble`, `pickup_assemble`, `review_assemble`)
dispatches through its OWN closed `_CLI_DISPATCH` table of in-process
Python handlers (`apply_base.execute_directives`), never a dynamically-
resolved-and-imported `coordinator/bin/` CLI script with an argv-taking
`main` — so the prog-slot-skip bug class this file guards structurally
cannot occur there (there is no `argv`/`main(argv)` dispatch to skip a
slot in). The three-name literal is therefore replaced with the
discovery call so a FOURTH ceremony-shaped package (should one ever define
its own `CONSUMES_MANIFEST`) is picked up automatically, with no hand-edit
here — never re-hand-derive the two facts above by inspection; grep in
this module docstring's discovery call as this file's own contemporaneous
oracle if a new package is ever suspected to be missing.

Two properties asserted per manifest member whose script resolves to a
file that exists on disk:
    1. It defines a top-level `main` function. A `main`-less CLI
       (`_cli`/other name) raises `UnrecognizedDirective` at dispatch time
       — the `coordinator-resolve-validation-cmd.py` shape this audit also
       found and fixed (it exposed `_cli`, not `main`, so
       `d_step2_resolve_validation_cmd` — ungated, fires on every
       `/workweek-complete apply` run — always failed before ever reaching
       its own logic).
    2. If that `main` takes at least one parameter (the argv-taking shape,
       as opposed to a zero-arg `main()` trampoline that reads `sys.argv`
       itself), its body does not slice `argv[1:]` (the prog-slot
       off-by-one) without ever consuming `argv[0]` (the shape a
       legitimate subcommand router — `session-claim-cli`, `archive-
       stamp-cli`, `coordinator-initiative`, etc. — uses instead, where
       `argv[0]` IS the real first token, consumed as a subcommand name,
       and `argv[1:]` is the genuine "rest of the args after the
       subcommand" — not a prog-name skip).

Spec backlink: docs/plans/2026-07-26-workstream-complete-computed-frontage.md, chunk C4
"""

from __future__ import annotations

import ast
import io
import re
import tokenize
from pathlib import Path

import importlib

import pytest

from coordinator_core.ceremony_common.phantom_resolves_sweep import discover_consumes_manifest_modules

_BIN_ROOT = Path(__file__).resolve().parents[2] / "coordinator" / "bin"


def _discover_all_manifest_names() -> tuple[str, ...]:
    """Union of every `CONSUMES_MANIFEST` this repo's `coordinator_core`
    tree defines, discovered by source scan rather than a hand-typed
    import list — see this module's docstring for why exactly three
    packages qualify today and why the other `brief(`-defining packages
    structurally cannot (`_CLI_DISPATCH`-shaped, not `CONSUMES_MANIFEST`-
    shaped). A package name appearing in more than one manifest (e.g.
    `emit-cadence`, `coordinator-queue-append`) is deduplicated by the set
    union."""
    coordinator_core_root = Path(__file__).resolve().parents[1]
    names: set[str] = set()
    for _package, path in discover_consumes_manifest_modules().items():
        rel = path.relative_to(coordinator_core_root)  # <pkg>/<file>.py, relative to coordinator_core/
        parts = rel.with_suffix("").parts
        if parts[-1] == "__init__":
            parts = parts[:-1]
        module_name = "coordinator_core." + ".".join(parts)
        module = importlib.import_module(module_name)
        names.update(getattr(module, "CONSUMES_MANIFEST"))
    return tuple(sorted(names))


_ALL_MANIFEST_NAMES: tuple[str, ...] = _discover_all_manifest_names()

#: Matches a raw `argv[1:]` slice — deliberately excludes `sys.argv[1:]`
#: (the `if argv is None else argv` fallback idiom several CLIs use to
#: default to the real process argv when called with no override, which
#: is an unrelated, correct pattern) via the negative lookbehind.
_SLICE_RE = re.compile(r"(?<!sys\.)\bargv\[1:\]")

#: Matches `argv[0]` used anywhere in the body — evidence `argv[1:]`
#: elsewhere in the SAME body is a legitimate "subcommand, then rest"
#: split, not a prog-slot skip.
_IDX0_RE = re.compile(r"(?<!sys\.)\bargv\[0\]")


def _resolve_script(name: str) -> Path:
    """Two-candidate literal lookup (mirrors `workstream_complete.apply.
    _resolve_script_path`/`workweek_complete.apply`'s identical helper): a
    manifest member ships as either `<name>.py` or an extensionless
    bareword launcher shim. Not a glob/search."""
    if name.endswith(".py"):
        return _BIN_ROOT / name
    py_path = _BIN_ROOT / f"{name}.py"
    if py_path.exists():
        return py_path
    return _BIN_ROOT / name


def _find_top_level_main(tree: ast.Module) -> ast.FunctionDef | None:
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == "main":
            return node
    return None


def _code_only(body_src: str) -> str:
    """Strips comments AND string-literal contents (docstrings, f-strings,
    ordinary quoted args) from `body_src`, leaving only executable-code
    tokens for pattern matching. Necessary because this module's own
    explanatory comments/docstrings (e.g. wsc-tail.py's `main()` docstring,
    added by this very audit, prose-mentions "argv[0]"/"argv[1:]" while
    describing the fix) would otherwise false-negative the regex match —
    confirmed empirically: scanning raw source (comments+docstrings
    included) failed to redden when the fix was reverted for a red-proof
    run, because the surviving explanatory comment text satisfied the
    `argv[0]`-present exemption on its own. STRING tokens are blanked to a
    fixed placeholder (not deleted) so line/column structure — irrelevant
    here, since only substring matching is done on the result — never
    causes a tokenize resync issue; COMMENT tokens are dropped outright.
    """
    out: list[str] = []
    prev_alnum = False
    try:
        tokens = tokenize.generate_tokens(io.StringIO(body_src).readline)
        for tok in tokens:
            if tok.type == tokenize.COMMENT:
                continue
            text = '""' if tok.type == tokenize.STRING else tok.string
            if not text:
                continue
            # Only insert a separating space between two adjacent
            # alphanumeric-ish tokens (e.g. two keywords/names that would
            # otherwise merge into one word) -- NOT between every token, or
            # `argv[1:]` reconstructs as `argv [ 1 : ]`, which the plain
            # `argv\[1:\]` regex below no longer matches at all (silently
            # defeating this entire guard -- caught empirically during the
            # red-proof run for this file).
            cur_alnum = text[0].isalnum() or text[0] == "_"
            if out and prev_alnum and cur_alnum:
                out.append(" ")
            out.append(text)
            prev_alnum = cur_alnum
    except (tokenize.TokenError, IndentationError, SyntaxError):
        # A dedent-relative snippet (as opposed to a full file) can trip
        # tokenize's indentation bookkeeping in rare cases -- fall back to
        # the raw text rather than skipping the check entirely; this only
        # widens what a comment/docstring could false-negative, it never
        # hides a real violation.
        return body_src
    return "".join(out)


@pytest.mark.parametrize("cli_name", _ALL_MANIFEST_NAMES)
def test_manifest_cli_exposes_a_main_entrypoint(cli_name: str) -> None:
    """Every consumes-manifest CLI that resolves to a real file on disk
    must define a top-level `main` — the one attribute name every apply.py's
    `_invoke_cli_main` looks up via `getattr(module, "main", None)`. A
    resolved-but-missing file (the documented, tolerated dispatch-table gap
    — e.g. `scan_unresolved_ubt_records.py` historically, `coordinator-
    queue-append` under `workday_complete.apply`'s own non-bareword-aware
    resolution) is a SEPARATE, already-tolerated failure mode (per-directive
    `FileNotFoundError`, not this test's concern) — skipped here rather than
    failed, since this file's job is the argv contract, not file presence.
    """
    script_path = _resolve_script(cli_name)
    if not script_path.exists():
        pytest.skip(f"{cli_name}: {script_path} does not exist on disk (documented gap)")

    tree = ast.parse(script_path.read_text(), filename=str(script_path))
    main_fn = _find_top_level_main(tree)
    assert main_fn is not None, (
        f"{cli_name} ({script_path}) exposes no top-level `main` function — "
        "every apply.py dispatcher's `_invoke_cli_main` will raise "
        "UnrecognizedDirective at dispatch time (getattr(module, 'main', "
        "None) is None), so any directive naming this cli always fails."
    )


@pytest.mark.parametrize("cli_name", _ALL_MANIFEST_NAMES)
def test_manifest_cli_argv_taking_main_never_skips_a_prog_slot(cli_name: str) -> None:
    """For every manifest member whose `main` takes at least one parameter
    (the argv-taking shape — as opposed to a zero-arg `main()` trampoline,
    which reads `sys.argv` itself and is out of this test's scope), its body
    must not slice `argv[1:]` without ever consuming `argv[0]` — the exact
    off-by-one this audit fixed in `wsc-tail.py`, `reconcile-completion-
    commits.py`, and `workweek-complete-close.py`. `apply.py`'s in-process
    dispatch calls `main_fn(list(args))` with NO argv[0] placeholder — an
    argv-taking `main` that assumes one is present silently eats the first
    real argument.
    """
    script_path = _resolve_script(cli_name)
    if not script_path.exists():
        pytest.skip(f"{cli_name}: {script_path} does not exist on disk (documented gap)")

    source = script_path.read_text()
    tree = ast.parse(source, filename=str(script_path))
    main_fn = _find_top_level_main(tree)
    if main_fn is None:
        pytest.skip(f"{cli_name}: no top-level main() — covered by the sibling entrypoint test")

    n_params = len(main_fn.args.posonlyargs) + len(main_fn.args.args)
    if n_params == 0:
        pytest.skip(f"{cli_name}: zero-arg main() trampoline (reads sys.argv itself) — not in scope")

    raw_body_src = ast.get_source_segment(source, main_fn) or ""
    code_src = _code_only(raw_body_src)
    has_slice = bool(_SLICE_RE.search(code_src))
    has_idx0 = bool(_IDX0_RE.search(code_src))
    assert not (has_slice and not has_idx0), (
        f"{cli_name} ({script_path}) slices `argv[1:]` in main() without ever "
        "consuming `argv[0]` — this is the prog-slot-skip shape apply.py's "
        "in-process dispatch never supplies a placeholder for (see this "
        "module's docstring); either treat argv[0] as a real token "
        "(subcommand/positional) or use `argv` directly, matching every "
        "other consumes-manifest CLI's convention."
    )
