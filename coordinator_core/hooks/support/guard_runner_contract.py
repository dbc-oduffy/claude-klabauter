"""The guard-on-runner contract: what an enrolled write-path guard must (and
must not) do so an in-process runner (`guard_runner.py`) can batch multiple
guards inside ONE Python interpreter safely, and what the aggregation,
exception-isolation, lazy-import, sys.path-ordering, and measurement-mode
seams around it guarantee.

Ported from DoE-claude `coordinator/hooks/scripts/_guard_runner_contract.py`
per docs/plans/2026-09-18-doe-holds-no-scripts.md chunk W4-C3, verbatim:
this module is pure vocabulary (dataclasses, constants, a scope-matching
predicate) with no per-guard-body reference that needs porting-time
adaptation -- the `ENROLLED_GUARD_MODULES` list and the two descriptor
constants below name guard bodies that are a LATER wave's `writes:`
(W4-C5/C6), landing under `coordinator_core/hooks/<name>.py`, not this
chunk's footprint; they are carried here unchanged as the same contract
vocabulary those future bodies conform to, same as every other reference in
this module to a `coordinator/`-prefixed doctrine-plane governed tree
(`coordinator/skills/`, `coordinator/docs/wiki/`, ...), which is accurate
guard SCOPE, not an import.

Import-free at module scope beyond the standard library: this module is
imported on the hot hook path (every edit), so it carries only
constants/enums/dataclasses, never guard logic.

--------------------------------------------------------------------------
CONTRACT MINIMUM (every enrolled guard must satisfy all of the following)
--------------------------------------------------------------------------

1. ENTRYPOINT SHAPE. A guard exposes `main() -> int`. `main()` MUST NOT use
   `sys.exit()` for control flow -- the runner calls `main()` directly and
   inspects its return value; it never wraps the call in a
   `try/except SystemExit` to recover a control-flow exit. The guard's own
   `__main__` block MAY still read `sys.exit(main())` (this is what keeps
   the guard independently invocable as a standalone script -- see clause
   9, MEASUREMENT MODE), but that block is never reached when the runner
   calls `main()` in-process.

2. NO `os._exit`. Unrecoverable -- `os._exit()` bypasses cleanup and kills
   the whole hook process mid-run, taking down every OTHER guard batched in
   the same runner invocation along with it.

3. NO `atexit` HANDLER REGISTRATION. A guard's `atexit` handler would fire
   at hook-PROCESS exit, not at the guard's own logical end -- registering
   one leaks guard-local cleanup timing into every subsequent guard's run
   inside the same process and into the process's own shutdown.

4. NO CWD MUTATION; NO `sys.path` MUTATION FROM GUARD CODE. A guard runs
   batched with siblings inside one process -- `os.chdir()` or an
   uncoordinated `sys.path` write inside guard logic corrupts every guard
   that runs after it in the same batch. (The RUNNER may perform its own
   sys.path setup once, at import-scope discovery time -- see clause 8,
   SYS.PATH ORDERING -- but that is runner-owned, not guard-owned, and
   happens before any guard's `main()` executes.)

5. NO CROSS-INVOCATION MODULE-GLOBAL STATE. A guard module must not
   accumulate state in a module-level global across separate `main()`
   invocations within the same process lifetime.

6. STDERR CAPTURE. The runner captures each guard's stderr output
   per-guard; a guard's stderr text never reaches the real stderr stream
   directly under the runner.

7. IMPORT SIDE-EFFECT FREEDOM. Importing a guard module (the module-scope
   code that runs on `import guard_module`, before any function is called)
   must be side-effect-free -- no I/O, no env mutation, no state
   registration. The runner may import many guard modules in sequence
   inside one process; an import-time side effect in one guard is invisible
   to review and corrupts every guard imported after it.

8. SYS.PATH PLACEMENT. The runner (not any individual guard) is responsible
   for any `sys.path` setup its own import resolution needs. Do NOT
   reintroduce a hand-rolled `sys.path.append`/`insert` inside a guard
   body -- placement is a runner concern.

9. MEASUREMENT MODE IS STANDALONE-INVOCATION-ONLY. `message_envelope.emit()`
   already special-cases `COORDINATOR_HOOK_MESSAGE_MEASURE=1` to write a
   structured measurement record instead of the real channel output. This
   contract does NOT specify a runner-side passthrough for that mode: the
   runner's own aggregation (clause 10) concatenates every guard's captured
   stdout into ONE additionalContext envelope, which would swallow or
   mangle a measurement record emitted by an individual guard mid-batch.
   Measurement mode is therefore standalone-invocation-only -- a guard's own
   `__main__` entry (clause 1) stays live specifically so measurement
   continues to work by invoking the guard as a standalone script, never
   through the runner's batched path.

--------------------------------------------------------------------------
RUNNER-SIDE GUARANTEES (properties the runner itself must hold; not
guard-authored, but every enrolled guard depends on them)
--------------------------------------------------------------------------

10. AGGREGATION IS CLASS-AWARE, NOT FIRST-DENY-WINS. Across one batch of
    guards run against one payload: at most one DENY reaches the harness --
    the FIRST guard whose verdict is `CHANNEL_DENY` short-circuits any
    REMAINING `CHANNEL_DENY` guards (they do not run, or their verdict is
    discarded if they already ran) -- but ALL `additionalContext` texts
    produced by every guard in the batch (deny-channel or not) are
    concatenated into a SINGLE `additionalContext` envelope, because stdout
    can carry only one hookSpecificOutput envelope per hook process. A deny
    and an advisory firing on the SAME payload must both reach the harness
    -- the deny via `permissionDecisionReason`, the advisory via the
    aggregated `additionalContext` text.

11. EXCEPTION ISOLATION. Each guard's `main()` call runs inside its own
    `try/except BaseException` (deliberately as broad as `BaseException`,
    not `Exception`, so a stray `SystemExit` or `KeyboardInterrupt` escaping
    guard code is also caught) -- on failure, the runner records the
    guard's name and CONTINUES to the next guard, rather than aborting the
    whole batch.

12. LAZY IMPORT IS TWO-STAGE. Each guard declares a cheap, dependency-free
    SCOPE DESCRIPTOR (see `GuardScopeDescriptor` below) that the runner
    evaluates WITHOUT importing the guard's body module; only a match
    against the descriptor triggers the guard's real import. This is
    required, not merely an optimization: a runner that imports every guard
    body up front to ask "am I in scope" reintroduces that import cost for
    every guard on every edit regardless of whether it is ever in scope.

    The descriptor must be import-free and live OUTSIDE the guard's own
    body module, because the naive "defer the whole guard import until the
    payload matches its scope" design is CIRCULAR: importing the guard to
    ask whether to import the guard defeats the lazy-import win entirely.

--------------------------------------------------------------------------
Shared vocabulary
--------------------------------------------------------------------------
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import PureWindowsPath
from typing import FrozenSet, Optional, Tuple

#: Greppable registry token for this contract.
TRIPWIRE_TOKEN = "GUARD-ON-RUNNER-CONTRACT"

#: The channel names a guard's verdict is expressed in, mirrored from
#: `message_envelope.py`'s `CHANNEL_STOP` / `CHANNEL_ADDITIONAL_CONTEXT` /
#: `CHANNEL_DENY` constants (NOT re-imported here -- this module stays
#: import-free at module scope beyond the standard library). Only
#: CHANNEL_ADDITIONAL_CONTEXT and CHANNEL_DENY are relevant to the
#: PreToolUse write-path runner this contract targets; CHANNEL_STOP is a
#: Stop-family shape out of scope here.
CHANNEL_ADDITIONAL_CONTEXT = "additional_context"
CHANNEL_DENY = "deny"

#: Environment variable that puts a guard's own `message_envelope.emit()`
#: call into measurement mode -- mirrored from `message_envelope.py`'s
#: `MEASURE_ENV_VAR` for the same import-free-module-scope reason as the
#: channel constants above. Per clause 9, the runner does NOT special-case
#: this variable: measurement mode is standalone-invocation-only.
MEASURE_ENV_VAR = "COORDINATOR_HOOK_MESSAGE_MEASURE"

#: Forbidden-construct grep patterns a conformance test applies to every
#: enrolled guard's source text, per clauses 2-4 and 8. Each value is a
#: plain substring/regex fragment, not a compiled pattern.
FORBIDDEN_OS_EXIT = r"os\._exit"
FORBIDDEN_ATEXIT = r"atexit\."
FORBIDDEN_CHDIR = r"os\.chdir"
#: A `sys.path.insert` occurring AFTER the module's own import block is
#: forbidden (clause 8); one at TOP of a module, before other imports, is
#: an existing self-resolution idiom and is exempt -- the conformance test
#: locates the import block's end and only flags a later occurrence.
FORBIDDEN_LATE_PATH_INSERT = r"sys\.path\.insert"


@dataclass(frozen=True)
class GuardScopeDescriptor:
    """The cheap, dependency-free scope predicate the runner evaluates
    WITHOUT importing a guard's body module (clause 12, LAZY IMPORT).

    `path_suffixes`: a frozenset of filename suffixes (e.g. `.py`, `.md`)
    the guard cares about; empty means "no suffix restriction" (checked by
    `directory_substrings` alone, if any).

    `directory_substrings`: a tuple of path substrings at least one of
    which must appear in the target path for the guard to be in scope;
    empty means "no directory restriction" (checked by `path_suffixes`
    alone, if any).

    `basenames`: a frozenset of exact filename basenames that match
    REGARDLESS of directory -- an OR alternative to the
    `path_suffixes`+`directory_substrings` pair, not a further restriction
    on it. Added because the two-field form above cannot express "suffix A
    confined to these dirs, OR suffix B matching anywhere": a repo-root
    config file carries no directory-prefixed segment, so it can never
    satisfy `directory_substrings` however that tuple is widened, and
    widening it to admit one repo-root file would also admit every OTHER
    path with that suffix at any repo root -- a real over-match.
    `basenames` matching is separator-normalized the same way
    `directory_substrings` is (see `matches()`).

    A descriptor with ALL THREE fields empty is never in scope (matches
    nothing) -- an enrolled guard must declare at least one restriction, or
    its entry is a bug (it would defeat lazy import by always matching).
    """

    guard_module: str
    path_suffixes: FrozenSet[str] = field(default_factory=frozenset)
    directory_substrings: Tuple[str, ...] = ()
    basenames: FrozenSet[str] = field(default_factory=frozenset)

    def matches(self, target_path: Optional[str]) -> bool:
        """Pure, import-free scope check. `target_path` is the raw
        (possibly `None`) path string extracted from the hook payload --
        this function does no filesystem I/O and imports nothing beyond
        what this module already imports at the top.

        `directory_substrings` are declared forward-slash-only, but
        `target_path` is a raw payload string that on Windows is
        backslash-separated -- a bare `in` check against the declared
        substring silently under-matches every Windows call. Normalized to
        forward slashes for the directory-substring check only (host
        -neutral: a no-op on a POSIX path, which already uses `/`);
        `path_suffixes` needs no such normalization since `endswith` on a
        filename suffix does not depend on the separator.

        `basenames`, when declared, is checked FIRST and independently: a
        match there returns `True` immediately, regardless of
        `path_suffixes`/`directory_substrings` -- an OR, not an AND, with
        the suffix+directory pair. The basename is extracted from the same
        separator-normalized path the directory-substring check uses, so a
        config-file target matches on both POSIX and backslash-separated
        Windows payload strings.

        Residual, unclosed by this or any separator-normalization scheme:
        `PureWindowsPath` parses `\\` as a separator unconditionally, on
        every host, so a POSIX path whose leaf genuinely contains a literal
        backslash character (legal, if unusual, on POSIX) is still mangled
        here. This function does not claim to close that case; it only
        fixes the Windows-payload under-match described above.
        """
        if not target_path:
            return False
        if not self.path_suffixes and not self.directory_substrings and not self.basenames:
            return False

        if self.basenames:
            normalized_path = PureWindowsPath(target_path).as_posix()
            basename = normalized_path.rsplit("/", 1)[-1]
            if basename in self.basenames:
                return True
            if not self.path_suffixes and not self.directory_substrings:
                return False

        suffix_ok = True
        if self.path_suffixes:
            suffix_ok = any(target_path.endswith(suf) for suf in self.path_suffixes)
        if not suffix_ok:
            return False
        if self.directory_substrings:
            normalized_path = PureWindowsPath(target_path).as_posix()
            return any(sub in normalized_path for sub in self.directory_substrings)
        return True


#: The enrolment list a conformance test sources its guard corpus from.
#: Filenames only (no directory prefix) -- NAMES the future hook-body
#: modules W4-C5/C6 land, not files present in this chunk's footprint. See
#: this module's own docstring, "ported ... verbatim", for why the list is
#: carried here unchanged rather than emptied: it is contract vocabulary a
#: later wave's bodies must conform to, not a live import target of this
#: module.
ENROLLED_GUARD_MODULES: Tuple[str, ...] = (
    "guard-oss-payload-locality.py",
    "nudge-plan-test-surface-tier.py",
    "guard-prompt-surface-citations.py",
    "check-claude-md-size.py",
    "guard-doctrine-changelog-prose.py",
    "guard-test-tree-git-fixture-spawn.py",
    "guard-python-syntax-on-write.py",
    "guard-doctrine-surface-ratio.py",
    "guard-posix-invocation-doctrine-write.py",
    "guard-handoff-summary-cap-on-write.py",
)

#: `guard-doctrine-changelog-prose.py`'s `GuardScopeDescriptor`. The guard's
#: REAL scope is a `.md` file under one of five fixed governed doctrine
#: trees, or a `*.schema.json` file directly inside the doctrine schemas
#: dir, or a repo-root `coordinator.local.md` -- both reachable only by
#: importing that (not-yet-landed) module, exactly the cost this descriptor
#: exists to defer. This descriptor is a strict superset (it does not
#: additionally exclude the `tests/`/`fixtures/` subdirectory carve-out the
#: real predicate applies, nor enforce a "direct children only"
#: restriction), so it can never under-match.
DOCTRINE_CHANGELOG_PROSE_SCOPE_DESCRIPTOR = GuardScopeDescriptor(
    guard_module="guard-doctrine-changelog-prose.py",
    path_suffixes=frozenset({".md", ".schema.json"}),
    directory_substrings=(
        "coordinator/skills/",
        "coordinator/agents/",
        "coordinator/commands/",
        "coordinator/snippets/",
        "coordinator/docs/wiki/",
        "coordinator/schemas/",
    ),
    basenames=frozenset({"coordinator.local.md"}),
)


#: `guard-doctrine-surface-ratio.py`'s `GuardScopeDescriptor`. The guard's
#: REAL scope is a `.md` file under one of the same five governed doctrine
#: trees (`.schema.json` is NOT one of the measured surfaces this guard
#: prices, unlike the changelog-prose guard's own scope, so it is
#: deliberately absent from `path_suffixes` here). A strict superset of the
#: real predicate, so it can never under-match.
GUARD_DOCTRINE_SURFACE_RATIO_SCOPE_DESCRIPTOR = GuardScopeDescriptor(
    guard_module="guard-doctrine-surface-ratio.py",
    path_suffixes=frozenset({".md"}),
    directory_substrings=(
        "coordinator/skills/",
        "coordinator/agents/",
        "coordinator/commands/",
        "coordinator/snippets/",
        "coordinator/docs/wiki/",
    ),
)


#: `check-claude-md-size.py`'s `GuardScopeDescriptor`. The guard's REAL
#: scope is the union of a size-budget check (a governed `CLAUDE.md` at
#: exactly two locations) and an admission-gate check (a small fixed set of
#: governed authoring surfaces). Every one of those concrete paths ends
#: with exactly one of three basenames -- `CLAUDE.md`,
#: `em-operating-doctrine.md`, `agent-role-dispatched.md` -- the tightest
#: sound superset available without importing either predicate's machinery.
#: This guard's verdict travels via captured STDERR, not the stdout-JSON
#: envelope the other enrolled guards use -- see `RegisteredGuard`'s own
#: docstring in `guard_runner.py` for that seam.
CHECK_CLAUDE_MD_SIZE_SCOPE_DESCRIPTOR = GuardScopeDescriptor(
    guard_module="check-claude-md-size.py",
    path_suffixes=frozenset(
        {"CLAUDE.md", "em-operating-doctrine.md", "agent-role-dispatched.md"}
    ),
)
