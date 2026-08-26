"""coordinator/bin/tests/test_publish_sync_override_seam_parity.py — report
which percolate roots on THIS machine carry a `publish_sync.py` override that
Claude-klabauter's own dispatch contract would refuse.

WHY THIS EXISTS AT ALL, given `check_publish_sync_contract` already refuses
fail-closed at round time: that guard fires at the wrong MOMENT, not the wrong
depth. The drift is introduced here — claude-klabauter adds a keyword-only parameter to
`coordinator/lib/percolate/publish_sync.py` — and is not detectable until some
other repo, hours later, runs a round and takes a FATAL. Observed 2026-08-26:
Claude-klabauter's `sweep_top_level_orphans`/`copy_file`/`renamed_dir_names`/
`renamed_file_names` additions left BOTH of DoE-claude's copies (`setup/` and
`coordinator/templates/setup/`, each a contract-only row keeping its own
resolver ladder) two parameters behind. Their own parity oracle was red on
precisely this and nobody had run it, because the person who needs it is
whoever runs the next round in a different repo. A guard that has to be
remembered is the guard that gets forgotten; this one fires in the tree whose
change causes the break.

WHAT IT ASSERTS, and the word matters: that every resolvable override WOULD
NOT REFUSE — never that it is "in sync". A signature-shaped patch satisfies a
signature oracle while silently not sweeping, which is exactly the trap
doe-claude-em avoided by porting bodies rather than parameters when they
unblocked their round. Bodies are out of scope here and must stay out; this
answers only "would `dispatch_mirror_like` be able to call it".

AST READS ONLY — never imports the override. A percolate root's
`publish_sync.py` is another repo's file; importing it to read a signature
executes it, in a test, on a box running 50-70 peer sessions. `ast.parse` over
a handful of small files costs no process and no import side effect, and is
strictly cheaper than the `inspect.signature` path the round-time guard takes
(which is correct THERE — it has already imported the module to call it).

Run: python -m pytest coordinator/bin/tests/test_publish_sync_override_seam_parity.py -q
"""

from __future__ import annotations

import ast
import importlib.util
import sys
from pathlib import Path

import pytest

_BIN_DIR = Path(__file__).resolve().parents[1]
_COORDINATOR_LIB = _BIN_DIR.parent / "lib"
if str(_COORDINATOR_LIB) not in sys.path:
    sys.path.insert(0, str(_COORDINATOR_LIB))

from percolate.publish_modes import PUBLISH_MODES  # noqa: E402


def _load_publish_module():
    """Load `publish.py` for its `_read_doe_root_pointer` rung ladder rather
    than re-deriving the pointer read here — a second copy of that ladder is
    the drift this file exists to catch, one layer up."""
    spec = importlib.util.spec_from_file_location(
        "publish_override_seam_parity_under_test", _BIN_DIR / "publish.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


publish = _load_publish_module()

#: Where a percolate root keeps its own copy, relative to the root. Both are
#: real: `setup/` is the one a round resolves through
#: `_resolve_publish_sync_module_path`, and `coordinator/templates/setup/` is
#: the template that seeds the next installed root — a stale template hands the
#: same refusal to every future consumer, so it is checked identically.
_OVERRIDE_RELATIVE_PATHS = (
    Path("setup") / "publish_sync.py",
    Path("coordinator") / "templates" / "setup" / "publish_sync.py",
)


def _declared_roots() -> "list[Path]":
    """Percolate roots resolvable on this box. Today that is the `.doe-root`
    pointer; the list is a function rather than a constant so a second root
    joins by being resolvable, not by editing an assertion."""
    roots: "list[Path]" = []
    pointer = publish._read_doe_root_pointer()
    if pointer:
        roots.append(Path(pointer))
    return roots


def _resolvable_overrides() -> "list[Path]":
    return [
        root / relative
        for root in _declared_roots()
        for relative in _OVERRIDE_RELATIVE_PATHS
        if (root / relative).is_file()
    ]


def _accepted_keywords(source: str, symbol: str) -> "tuple[bool, set[str], bool]":
    """`(defined, accepted_keyword_names, is_bare_var_wrapper)` for `symbol`.

    A `**kwargs` catch-all reports as a bare wrapper only when it is the
    function's ONLY parameter shape, matching `check_publish_sync_contract`'s
    own rule: a bare `(*args, **kwargs)` passes a superficial check and still
    fails at runtime, so it must not read as acceptance here either.
    """
    tree = ast.parse(source)
    for node in tree.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if node.name != symbol:
            continue
        args = node.args
        named = {a.arg for a in args.args} | {a.arg for a in args.kwonlyargs}
        named |= {a.arg for a in args.posonlyargs}
        has_var_kw = args.kwarg is not None
        is_bare = not named and (has_var_kw or args.vararg is not None)
        if has_var_kw and not is_bare:
            # A real signature that also carries `**kwargs` absorbs anything;
            # the round-time guard's `bind_partial` accepts that too.
            return True, named | {"**"}, False
        return True, named, is_bare
    return False, set(), False


def _would_refuse(override_path: Path) -> "list[str]":
    """The reasons a round would refuse this override, empty when it would
    not. Mirrors `check_publish_sync_contract`'s obligations in the order that
    guard checks them, minus the run-scoping — a template or a root is checked
    against EVERY entry point, because we cannot know which modes a future
    round's rows will dispatch."""
    reasons: "list[str]" = []
    source = override_path.read_text(encoding="utf-8", errors="replace")
    for descriptor in PUBLISH_MODES:
        if descriptor.entry_point is None:
            continue
        defined, accepted, is_bare = _accepted_keywords(source, descriptor.entry_point)
        if not defined:
            reasons.append(f"does not define {descriptor.entry_point!r}")
            continue
        if is_bare:
            reasons.append(
                f"{descriptor.entry_point!r} is a bare (*args/**kwargs) wrapper"
            )
            continue
        if "**" in accepted:
            continue
        missing = sorted(set(descriptor.bind_kwargs) - accepted)
        if missing:
            reasons.append(
                f"{descriptor.entry_point!r} does not accept {missing} "
                f"(mode {descriptor.wire_name!r})"
            )
    defined, accepted, is_bare = _accepted_keywords(source, "load_ignore")
    if not defined:
        reasons.append("does not define 'load_ignore'")
    elif is_bare:
        reasons.append("'load_ignore' is a bare (*args/**kwargs) wrapper")
    return reasons


def test_no_resolvable_override_would_refuse_a_round():
    """The load-bearing assertion. Fails in the tree whose seam change breaks
    a downstream root, naming the root and the missing parameters — the two
    facts that made the 2026-08-26 diagnosis a five-minute job once someone
    finally saw the FATAL."""
    overrides = _resolvable_overrides()
    if not overrides:
        pytest.skip(
            "no percolate root resolves on this box (`.doe-root` unset or absent) — "
            "nothing to compare against; this is a machine fact, not a pass"
        )
    refusing = {
        str(path): reasons
        for path in overrides
        if (reasons := _would_refuse(path))
    }
    assert not refusing, (
        "percolate root(s) carry a publish_sync.py override that claude-klabauter's own "
        f"dispatch contract WOULD REFUSE: {refusing}. The seam changed here and "
        "these copies did not follow. Port the BODIES, not just the parameters — "
        "a signature-only patch satisfies this check while silently not doing "
        f"the work. Engine module: {publish._ENGINE_PUBLISH_SYNC_PATH}."
    )


def test_the_engine_module_satisfies_its_own_contract():
    """Pins the checker against the module it is checking others by. If this
    fails, `_accepted_keywords` has drifted from the real signatures and every
    other verdict in this file is worthless — a green suite that proves
    nothing is the failure mode a parity oracle is most prone to."""
    assert _would_refuse(publish._ENGINE_PUBLISH_SYNC_PATH) == []


def test_a_stale_override_is_caught(tmp_path):
    """The oracle's own teeth, independent of what happens to be on this box:
    the exact 2026-08-26 shape — `sync_mirror` two keyword-only parameters
    behind — must read as would-refuse."""
    stale = tmp_path / "publish_sync.py"
    stale.write_text(
        "def sync_mirror(src, dest, *, copy_file=None, renamed_dir_names=None):\n"
        "    pass\n"
        "def sync_flat_mirror(src, dest, *, copy_file=None):\n"
        "    pass\n"
        "def sync_repo_cut(src, dest, *, dry_run=False):\n"
        "    pass\n"
        "def load_ignore(path):\n"
        "    pass\n",
        encoding="utf-8",
    )
    reasons = _would_refuse(stale)
    assert len(reasons) == 1
    assert "renamed_file_names" in reasons[0]
    assert "sweep_top_level_orphans" in reasons[0]


def test_a_bare_wrapper_does_not_pass_as_acceptance(tmp_path):
    """`(*args, **kwargs)` binds anything and does nothing — the fail-open the
    round-time guard names explicitly. It must not read as parity here."""
    wrapper = tmp_path / "publish_sync.py"
    wrapper.write_text(
        "def sync_mirror(*args, **kwargs):\n    pass\n"
        "def sync_flat_mirror(*args, **kwargs):\n    pass\n"
        "def sync_repo_cut(*args, **kwargs):\n    pass\n"
        "def load_ignore(*args, **kwargs):\n    pass\n",
        encoding="utf-8",
    )
    reasons = _would_refuse(wrapper)
    assert len(reasons) == 4
    assert all("bare" in reason for reason in reasons)
