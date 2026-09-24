"""Tests for coordinator_core.bash_guards.block_subagent_grant_acquisition.

Covers the ACQUISITION-side leg closing the hole where a dispatched
subagent mints its own `granted_by: "pm"` CLAUDE.md write-grant record: the
`-m` module form (`python3 -m coordinator_core.session.claude_md_grant
grant ...`), the widened `-c` inline-import form (AC11), the identity gate
(raw `agent_id` presence, fail CLOSED on unresolvable -- AC1/AC2/AC3), the
`grant`-only subcommand gating (`read`/`check` allowed -- AC4), the `--agent`
EM-session regression pin (AC5, `agent_type`-only is NOT denied), the
dispatch-chain wiring (C2), and a scope-equals-enforcement check against the
module's own docstring (AC10, DR-104).

Pure Python -- no shell spawns, no git repo required.

Spec backlink: coordinator_core/bash_guards/block_subagent_grant_acquisition.py
Spec backlink: pln-discriminate-the-caller-on-the-bf406b, chunk C3
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

from coordinator_core.bash_guards import block_subagent_grant_acquisition as guard
from coordinator_core.bash_guards import dispatch
from coordinator_core.bash_guards import _verdict
from coordinator_core import hooks as _hooks_pkg
from coordinator_core import ops as _ops_pkg


def _payload(command, agent_id=None, agent_type=None):
    p = {
        "tool_name": "Bash",
        "tool_input": {"command": command},
        "session_id": "sess1",
        "cwd": "/repo",
    }
    if agent_id is not None:
        p["agent_id"] = agent_id
    if agent_type is not None:
        p["agent_type"] = agent_type
    return p


def _reason(out):
    assert out is not None, "expected a deny envelope, got allow"
    hso = out["hookSpecificOutput"]
    assert hso["hookEventName"] == "PreToolUse"
    assert hso["permissionDecision"] == "deny"
    return hso["permissionDecisionReason"]


_MODULE_M_GRANT = (
    'python3 -m coordinator_core.session.claude_md_grant grant pm "note"'
)
_DASH_C_GRANT = (
    "python3 -c \"from coordinator_core.session.claude_md_grant import "
    "write_claude_md_write_grant as w; w('pm', '...')\""
)
_DASH_C_READ = (
    "python3 -c \"from coordinator_core.session.claude_md_grant import "
    "read_claude_md_write_grant as r; r()\""
)


class TestNonBashOrEmpty:
    def test_non_bash_tool_allows(self):
        assert guard.check({"tool_name": "Edit", "tool_input": {"file_path": "x"}}) is None

    def test_empty_command_allows(self):
        assert guard.check(_payload("", agent_id="a1")) is None

    def test_malformed_tool_input_allows(self):
        assert guard.check({"tool_name": "Bash", "tool_input": "not-a-dict"}) is None


class TestDashMForm:
    def test_em_grant_allows(self):
        """AC2: no `agent_id` at all -> main-loop EM -> allowed."""
        assert guard.check(_payload(_MODULE_M_GRANT)) is None

    def test_subagent_grant_denies(self):
        """AC1: subagent (`agent_id` present) invoking `grant` -> DENY."""
        _reason(guard.check(_payload(_MODULE_M_GRANT, agent_id="a1")))

    def test_present_but_unresolvable_agent_id_denies(self):
        """AC3: fails CLOSED on a present-but-unresolvable `agent_id`."""
        _reason(
            guard.check(
                _payload(_MODULE_M_GRANT, agent_id="unresolvable-nonexistent-id")
            )
        )

    def test_subagent_read_allows(self):
        """AC4: `read` is not gated."""
        assert guard.check(
            _payload(
                "python3 -m coordinator_core.session.claude_md_grant read",
                agent_id="a1",
            )
        ) is None

    def test_subagent_check_allows(self):
        """AC4: `check` is not gated."""
        assert guard.check(
            _payload(
                "python3 -m coordinator_core.session.claude_md_grant check",
                agent_id="a1",
            )
        ) is None

    def test_lookalike_module_name_does_not_match(self):
        assert guard.check(
            _payload(
                "python3 -m coordinator_core.session.claude_md_grant_extra grant pm x",
                agent_id="a1",
            )
        ) is None
        assert guard.check(
            _payload(
                "python3 -m coordinator_core.session.claude_md_grantx grant pm x",
                agent_id="a1",
            )
        ) is None


class TestAgentTypeOnlyRegression:
    """AC5, LOAD-BEARING: the `--agent` EM-session regression the spike
    surfaced. `agent_type` alone (no `agent_id`) must NOT be denied -- this
    guard gates on raw `agent_id` presence alone, never the broader
    `resolve_effective_types` OR-resolved triple."""

    def test_agent_type_only_payload_not_denied(self):
        assert guard.check(
            _payload(_MODULE_M_GRANT, agent_type="coordinator:em")
        ) is None


class TestDashCForm:
    """AC11: widened inline-import `-c` shape."""

    def test_subagent_dash_c_grant_denies(self):
        _reason(guard.check(_payload(_DASH_C_GRANT, agent_id="a1")))

    def test_em_dash_c_grant_allows(self):
        assert guard.check(_payload(_DASH_C_GRANT)) is None

    def test_subagent_dash_c_read_allows(self):
        """A `-c` payload referencing only a read/check-shaped name mirrors
        the `-m` form's grant-only gating."""
        assert guard.check(_payload(_DASH_C_READ, agent_id="a1")) is None


class TestPowerShellParity:
    """2026-08-19 subagent-boundary MATCHERS widening: the same deny that
    fires under Bash must fire under PowerShell carrying the identical
    command -- the whole point of the widening (see module's MATCHERS
    comment and docs/reference/guard-tool-name-membership.md).
    """

    def test_subagent_grant_denies_via_powershell(self):
        p = _payload(_MODULE_M_GRANT, agent_id="a1")
        p["tool_name"] = "PowerShell"
        _reason(guard.check(p))

    def test_em_grant_allows_via_powershell(self):
        p = _payload(_MODULE_M_GRANT)
        p["tool_name"] = "PowerShell"
        assert guard.check(p) is None


class TestDispatchWiring:
    """C2: the leg is registered in `dispatch.py`'s CONFINEMENT_DENY run and
    is reachable through the dispatcher entrypoint, not merely callable in
    isolation -- this is the one thing C2 delivers that every other case in
    this file would pass identically against a completely unregistered
    guard."""

    def test_registered_in_confinement_deny_run(self):
        chain = dispatch._build_guard_chain(
            cmd="echo bash-guard-grant-acquisition-probe",
            session_id="probe-session",
            cwd="/tmp",
            payload={"tool_name": "Bash", "tool_input": {"command": "echo x"}},
            policy_file=None,
            host_is_windows=None,
        )
        entry = next(
            (e for e in chain if e.name == "block-subagent-grant-acquisition"), None
        )
        assert entry is not None, (
            "block-subagent-grant-acquisition missing from dispatch._build_guard_chain"
        )
        assert entry.band is dispatch.GuardBand.CONFINEMENT_DENY
        assert entry.fail_closed is True

    def test_reachable_through_dispatcher_entrypoint(self):
        payload = _payload(_MODULE_M_GRANT, agent_id="a1")
        out = dispatch.evaluate_payload_json(json.dumps(payload))
        assert out is not None, (
            "dispatcher entrypoint allowed a grant-acquisition invocation "
            "that check() alone denies -- leg not reachable through the "
            "dispatch chain"
        )
        hso = out["hookSpecificOutput"]
        assert hso["permissionDecision"] == "deny"


class TestScopeEqualsEnforcement:
    """AC10, DR-104: assert the leg denies EXACTLY the grant-acquisition
    invocations its own docstring claims to deny -- no wider, no narrower.
    Named precedent (DR-104, cited in the module docstring's HEURISTIC-NOT-
    EXHAUSTIVE section): `check_blanket_git_add`'s documented scope gap,
    where doctrine claimed a wider deny scope than the code enforced. This
    case exists so this leg does not repeat that gap.

    The docstring's RECOGNIZED SHAPES section claims exactly two positive
    shapes (`-m ... grant`, and `-c` referencing both the module and
    `write_claude_md_write_grant`) and explicitly disclaims exhaustiveness
    (HEURISTIC-NOT-EXHAUSTIVE) -- so this test does NOT assert coverage of
    an indirection route (script-to-disk-then-exec, alternate interpreter,
    etc.) the docstring itself says is not caught."""

    def test_denies_exactly_the_two_documented_shapes(self):
        # Shape 1: `-m` + module + `grant` subcommand.
        _reason(guard.check(_payload(_MODULE_M_GRANT, agent_id="a1")))
        # Shape 2: `-c` referencing both the module and the write function.
        _reason(guard.check(_payload(_DASH_C_GRANT, agent_id="a1")))

    def test_does_not_deny_a_command_merely_mentioning_the_module_name(self):
        """Not wider than documented: a command that references the module
        path in prose/echo, without either recognized invocation shape, is
        not classified."""
        assert guard.check(
            _payload(
                "echo coordinator_core.session.claude_md_grant",
                agent_id="a1",
            )
        ) is None

    def test_does_not_deny_dash_c_mentioning_module_without_write_func(self):
        """Not wider than documented: a `-c` payload referencing the module
        but not `write_claude_md_write_grant` is not grant-shaped."""
        assert guard.check(_payload(_DASH_C_READ, agent_id="a1")) is None

    def test_does_not_deny_dash_c_mentioning_lookalike_func_name(self):
        """Not wider than documented: a `-c` payload referencing an
        identifier that merely CONTAINS `write_claude_md_write_grant` as a
        substring (e.g. a hypothetical helper name) must not classify as
        grant-shaped -- word-boundary match, not substring containment."""
        payload = (
            "python3 -c \"from coordinator_core.session.claude_md_grant import "
            "_write_claude_md_write_grant_helper as w; w()\""
        )
        assert guard.check(_payload(payload, agent_id="a1")) is None

    def test_does_not_deny_non_python_command(self):
        """Not wider than documented: the docstring's RECOGNIZED SHAPES are
        both python-interpreter-headed; a non-python command is out of
        scope regardless of content."""
        assert guard.check(
            _payload(
                "rg 'claude_md_grant' coordinator_core/",
                agent_id="a1",
            )
        ) is None


class TestUnparseableCommandRecordsSilent:
    """An `_tokenize_full_command`-unparseable PowerShell shape must not
    silently return an unrecorded `None` -- it must declare a SILENT verdict
    on the out-of-band channel (`_verdict.record_silent`), so the miss is
    observable rather than indistinguishable from a genuine clean verdict."""

    _HERESTRING_APOSTROPHE = "$x = @'\nit's a note\n'@"
    _BACKTICK_UNTERMINATED = "python3 -c `\n  'unterminated payload"

    def test_powershell_herestring_records_silent(self):
        with _verdict.collecting() as silences:
            out = guard.check(_payload(self._HERESTRING_APOSTROPHE, agent_id="a1"))
        assert out is None
        assert _verdict.was_silent("block-subagent-grant-acquisition", silences)

    def test_powershell_backtick_continuation_records_silent(self):
        with _verdict.collecting() as silences:
            out = guard.check(_payload(self._BACKTICK_UNTERMINATED, agent_id="a1"))
        assert out is None
        assert _verdict.was_silent("block-subagent-grant-acquisition", silences)


# ---------------------------------------------------------------------------
# P169-C2: pin the grant writer out of the op registry.
#
# The bug row (state/bug-backlog/2026-09-11-block-subagent-grant-acquisition-
# is-door-blind.yaml) asked whether any registered op reaches the grant
# writer through `coordinator-invoke`. The census re-run over the full span
# (this plan's C2 body, not the narrower `ops/`+`invoke/` grep the original
# row cited) answers that with a static, `ast`-only transitive walk of the
# AUTHORITATIVE eager-import lists -- `coordinator_core.ops.
# _EAGER_OP_MODULES` and `coordinator_core.hooks._EAGER_HOOK_MODULES` --
# never `OP_MODULE_MAP`/`_registry_map`, whose own docstring disclaims being
# a correctness gate and whose package-shaped values (e.g.
# `coordinator_core.hooks`) never descend into the submodule that actually
# holds an op.
#
# No import is ever EXECUTED here -- every module is read as text and parsed
# with `ast.parse` only, so this walk cannot itself trigger the very
# registration side effects `coordinator_core.ops`/`coordinator_core.hooks`
# go to such lengths to keep lazy.
# ---------------------------------------------------------------------------

_GRANT_MODULE_DOTTED = "coordinator_core.session.claude_md_grant"
_GRANT_WRITE_FUNC = "write_claude_md_write_grant"

#: `coordinator_core/bash_guards/tests/` -> `coordinator_core/`.
_COORDINATOR_CORE_ROOT = Path(guard.__file__).resolve().parent.parent


def _module_imports(tree: ast.AST):
    """Yield `(dotted_module_or_None, imported_names)` for every
    `import`/`from ... import ...` statement in `tree`. A relative import
    (`level > 0`) yields `(None, [])` -- out of scope: `coordinator_core`
    uses absolute imports throughout."""
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield alias.name, []
        elif isinstance(node, ast.ImportFrom):
            if node.module is None or node.level:
                continue
            yield node.module, [alias.name for alias in node.names]


def _references_grant_writer(tree: ast.AST) -> bool:
    """True if `tree` itself imports-of or calls-to the grant WRITE function
    -- never a bare string mention of the module path, which would
    false-positive on `guard_doctrine_surface_bash_write.py`'s own
    `_CLAUDE_MD_GRANT_MODULE` string constant (a reference for its carve-out
    matcher, not a reach)."""
    for module, names in _module_imports(tree):
        if module == _GRANT_MODULE_DOTTED and _GRANT_WRITE_FUNC in names:
            return True
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            name = getattr(func, "attr", None) or getattr(func, "id", None)
            if name == _GRANT_WRITE_FUNC:
                return True
    return False


def _module_file(dotted: str, root: Path) -> "Path | None":
    """Resolve a dotted `coordinator_core...` module path to its `.py` file
    under `root` (the `coordinator_core` package directory), preferring a
    plain module file and falling back to a package `__init__.py`. Returns
    `None` for anything outside `coordinator_core` or that resolves to no
    file on disk (e.g. a C-extension or namespace package)."""
    parts = dotted.split(".")
    if not parts or parts[0] != "coordinator_core":
        return None
    rest = parts[1:]
    if not rest:
        return root / "__init__.py" if (root / "__init__.py").is_file() else None
    base = root
    for part in rest[:-1]:
        base = base / part
    module_file = base / (rest[-1] + ".py")
    if module_file.is_file():
        return module_file
    pkg_init = base / rest[-1] / "__init__.py"
    if pkg_init.is_file():
        return pkg_init
    return None


def _reachable_modules(entry_dotted_names, root: Path, source_override=None):
    """BFS the `coordinator_core` static import graph from `entry_dotted_
    names`, returning `{dotted: ast.AST}` for every module reached (never
    leaving the `coordinator_core` namespace). `source_override` is an
    optional `{dotted: source_text}` map consulted BEFORE any disk read --
    the planted-violation self-test below uses it to simulate a rogue
    transitive import with no fixture file written to disk."""
    source_override = source_override or {}
    visited: set = set()
    queue = list(entry_dotted_names)
    modules_seen: dict = {}
    while queue:
        dotted = queue.pop()
        if dotted in visited:
            continue
        visited.add(dotted)
        if dotted in source_override:
            source = source_override[dotted]
            label = f"<override:{dotted}>"
        else:
            path = _module_file(dotted, root)
            if path is None:
                continue
            try:
                source = path.read_text(encoding="utf-8")
            except OSError:
                continue
            label = str(path)
        try:
            tree = ast.parse(source, filename=label)
        except SyntaxError:
            continue
        modules_seen[dotted] = tree
        for module, _names in _module_imports(tree):
            if module and module.startswith("coordinator_core") and module not in visited:
                queue.append(module)
    return modules_seen


class TestGrantWriterOutOfOpRegistry:
    """P169-C2: the door-gap census, re-run over the AUTHORITATIVE eager-
    import lists (never `_registry_map.py`, a performance optimization
    explicitly disclaimed as a correctness gate)."""

    def test_no_eager_op_or_hook_module_transitively_reaches_grant_writer(self):
        entries = [module for module, _note in _ops_pkg._EAGER_OP_MODULES]
        entries += list(_hooks_pkg._EAGER_HOOK_MODULES)
        modules = _reachable_modules(entries, _COORDINATOR_CORE_ROOT)
        offenders = sorted(
            dotted for dotted, tree in modules.items() if _references_grant_writer(tree)
        )
        assert offenders == [], (
            "the eager op/hook import graph now transitively reaches the "
            f"grant WRITE function via: {offenders} -- exposing the grant "
            "on the coordinator-invoke door requires "
            "block_subagent_grant_acquisition to gain coordinator-invoke "
            "coverage (reuse block_subagent_commit's "
            "_invoke_op_token_indices) in the same change"
        )

    def test_planted_violation_is_detected_without_a_fixture_file(self):
        """A rogue eager-import-reachable module that imports the grant
        WRITE function two hops deep from its entry point IS flagged --
        proving the walk is transitive, not merely a check of the entry
        module's own imports. No file is written to disk (`source_
        override`)."""
        fake_entry = "coordinator_core._p169c2_planted_entry"
        fake_leaf = "coordinator_core._p169c2_planted_leaf"
        overrides = {
            fake_entry: f"import {fake_leaf}\n",
            fake_leaf: (
                f"from {_GRANT_MODULE_DOTTED} import {_GRANT_WRITE_FUNC}\n"
                "def rogue():\n"
                f"    {_GRANT_WRITE_FUNC}('pm', 'self-minted')\n"
            ),
        }
        modules = _reachable_modules(
            [fake_entry], _COORDINATOR_CORE_ROOT, source_override=overrides
        )
        offenders = [
            dotted for dotted, tree in modules.items() if _references_grant_writer(tree)
        ]
        # `fake_leaf` imports the grant module by name, so the walk also
        # reaches the real `claude_md_grant.py` on disk (which calls its own
        # write function from its CLI trampoline) -- both are legitimately
        # flagged; the assertion is that the PLANTED two-hop leaf is among
        # them, proving transitivity, not that it is the only offender.
        assert fake_leaf in offenders

    def test_string_mention_only_is_not_flagged(self):
        """Mirrors `guard_doctrine_surface_bash_write.py`'s own
        `_CLAUDE_MD_GRANT_MODULE` string constant: a bare string mention of
        the module path, with no import-of or call-to the WRITE function,
        must not classify as a reach."""
        tree = ast.parse(
            f'_CLAUDE_MD_GRANT_MODULE = "{_GRANT_MODULE_DOTTED}"\n'
        )
        assert _references_grant_writer(tree) is False

    def test_read_or_check_import_alone_is_not_flagged(self):
        """A module importing only the read/check functions (as
        `block_unauthorized_claude_md_write.py` does today) is not
        grant-shaped -- mirrors this guard's own `-c` subcommand
        discrimination."""
        tree = ast.parse(
            f"from {_GRANT_MODULE_DOTTED} import check_claude_md_write_grant\n"
        )
        assert _references_grant_writer(tree) is False
