"""coordinator_core.bash_guards._sentinel_removal_guard -- shared detection
engine for a Bash-level "this sentinel must not be removed/relocated by any
agent" guard, mirroring `_sentinel_creation_guard.SentinelCreationDetector`'s
architecture (same tokenizer/segmenter/indirection substrate) so a second
protected removal-sentinel is a target basename plus a deny/advisory message
away.

WHY THIS EXISTS. `.coordinator-dev-repo` sits at the DoE-claude repo root.
Its PRESENCE is the dev-vs-OSS discriminant consumed by
`coordinator_core.claude_md_budget` (`DEV_REPO_SENTINEL`),
`coordinator_core.resolve_coordinator_clone`, and written at install time by
`coordinator_core.install.maximalist`. Moving or deleting it destroys that
discriminant fleet-wide with NO error at the moment of the move -- the
breakage surfaces later, in an unrelated session that silently resolves the
wrong dev/OSS branch of logic.

`_sentinel_creation_guard.py` and `_sentinel_write_guard.py` both protect a
sentinel whose ABSENCE gates a capability, so they block CREATION; both
explicitly document removal as out of scope. This module is the mirror
image: it protects a sentinel whose PRESENCE is load-bearing, so it blocks
REMOVAL/RELOCATION instead -- `rm`/`unlink`, `mv` (sentinel as the SOURCE
argument only -- the sentinel as a MOVE DESTINATION is a creation shape,
squarely `_sentinel_creation_guard`'s territory, not this module's), `git
rm`/`git mv` (source), `find ... -delete` / `find ... -exec rm`, and a
`python -c` payload that both mentions the sentinel basename and calls a
removal/move verb (`remove`, `unlink`, `rmtree`, `move`, `rename`,
`replace`) -- narrowly, so a payload that merely READS the sentinel
(`exists`, `is_file`, as `resolve_coordinator_clone.py` and
`claude_md_budget.py` legitimately do) never denies.

REUSED PRIMITIVES (per module docstring convention already established by
`_sentinel_creation_guard.py`): `_tokenize_full_command`,
`_segments_from_tokens`, `_normalize_executable_basename`,
`_normalize_interpreter_basename`, `_strip_env_prefix`,
`_strip_heredoc_bodies`, `_has_noexec_flag_before_script`, `_has_script_operand`,
`_BUNDLED_C_FLAG_RE`, `_C_FLAG_INTERPRETERS`, `_SHELL_FILE_INTERPRETERS`,
`_MAX_INDIRECTION_DEPTH`, and `_skip_wrapper_own_argv` -- all imported from
`block_subagent_destructive_action.py` / `_command_tokenizer.py` exactly as
the sibling creation-detector imports them, not re-derived. This module's
own `_env_skip_index` mirrors (not imports -- it is a method on the
sibling's own class, not a free function) that detector's leading-`VAR=`/
`env`/passthrough-wrapper/`{`/`(` grouping walk, so both detectors agree on
what "the real argv0 position" means for a segment.

POSTURE -- DELIBERATELY THE OPPOSITE ASYMMETRY FROM THE SIBLING CREATION
DETECTOR. `_sentinel_creation_guard.py` defaults to DENY on ambiguity
because a false negative there breaks a security boundary (an agent
self-granting an override it should never hold). This guard defends against
EAGERNESS, not an adversary: a false negative here (a removal that slips
through) is still recoverable by hand the moment it is noticed, but a false
positive blocks ordinary file work in a busy shared repo -- `rm`, `mv`, and
`git mv` are extremely common, low-stakes commands, and this sentinel is one
specific dotfile among thousands of ordinary targets. Concretely:

  - A DIRECT rule match (rule fired against the sentinel's own basename, as
    a literal argument to a top-level `rm`/`mv`/`git rm`/`git mv`/`find
    -delete`, or a `python -c` payload that BOTH mentions the basename AND
    calls a removal/move verb) still DENIES -- this module is confident
    about what it positively found.
  - Genuinely UNEXAMINABLE indirection (an `xargs`-assembled command, a
    bare-interpreter-invoked script file whose content is not visible, a
    stdin-piped shell interpreter, or the indirection-depth cap) resolves to
    an ADVISORY, not a deny -- the opposite of the sibling creation
    detector's choice for the identical shape. An advisory still surfaces
    the concern (and the override env var) without blocking a command this
    module simply cannot see into, most of which have nothing to do with
    the sentinel at all.
  - A genuinely unparseable command (unbalanced quoting) that still
    mentions the sentinel basename in its raw text is likewise an ADVISORY,
    not the sibling's deny -- same reasoning: this module could not confirm
    a real removal shape, only a textual mention.
  - ONE named exception to the line above, and the only shape in this module
    where an unexaminable command DENIES: a command past
    `_command_tokenizer._MAX_TOKENIZABLE_COMMAND_CHARS` that also mentions
    the sentinel basename. The eagerness argument does not cover it. That
    ceiling is ~8x the largest command-shaped string this project has ever
    classified, so "ordinary file work in a busy shared repo" does not
    produce a 64 KiB command; padding a `rm <sentinel>` past the ceiling to
    buy an ADVISORY (which renders as `permissionDecision: allow`) is the
    adversary shape this module otherwise says it is not defending against,
    and it costs nothing legitimate to refuse. See `_REASON_OVER_CEILING`.

`reason_class` values below are borrowed from `_sentinel_creation_guard.py`
(`REASON_DIRECT` / `REASON_INDIRECTION`) for terminology parity, but this
module's `evaluate()` returns a THREE-way verdict (`VERDICT_DENY` /
`VERDICT_ADVISORY` / `VERDICT_ALLOW`) rather than the sibling's two-way
`(deny: bool, ...)`, precisely to carry this posture difference: `REASON_
DIRECT` always pairs with `VERDICT_DENY`, and `REASON_INDIRECTION` pairs with
`VERDICT_ADVISORY` for every cause except the over-ceiling one named in the
POSTURE list above (`_REASON_OVER_CEILING`), which is the module's single
`REASON_INDIRECTION` + `VERDICT_DENY` pairing.

Message-safety note carried over unchanged from the sibling module: a
recursive indirection verdict can still bottom out in the direct branch's
target-naming string one level down (e.g. `bash -c "rm <sentinel>"` unwraps
to a direct match). A consumer surfacing `reason_kind` on the advisory path
MUST still redact the target basename before display, exactly as the
sibling creation guard's own consumer does.
"""

from __future__ import annotations

import re
import shlex
from typing import List, Optional, Tuple

#: `reason_class` values -- see module docstring "POSTURE" section.
REASON_DIRECT = "direct"
REASON_INDIRECTION = "indirection"

#: Three-way verdict returned by `evaluate()`.
VERDICT_DENY = "deny"
VERDICT_ADVISORY = "advisory"
VERDICT_ALLOW = "allow"

from coordinator_core.bash_guards.block_subagent_destructive_action import (
    _BUNDLED_C_FLAG_RE,
    _C_FLAG_INTERPRETERS,
    _MAX_INDIRECTION_DEPTH,
    _SHELL_FILE_INTERPRETERS,
    _has_noexec_flag_before_script,
    _has_script_operand,
    _normalize_executable_basename,
    _normalize_interpreter_basename,
    _segments_from_tokens,
    _strip_env_prefix,
    _strip_heredoc_bodies,
    _tokenize_full_command,
)
from coordinator_core.bash_guards._command_tokenizer import (
    _skip_wrapper_own_argv,
    exceeds_tokenizable_ceiling as _exceeds_tokenizable_ceiling,
)
from coordinator_core.bash_guards._dialect import Dialect, resolve_segments_for_dialect
from coordinator_core.bash_guards._verdict import record_silent

#: Registered guard identity threaded into `_verdict.record_silent` calls
#: below -- matches `block_dev_repo_sentinel_removal.py`'s own dispatch
#: registration name (`block-dev-repo-sentinel-removal-advisory`), not this
#: engine module's filename, so a SILENT declaration reads the same as any
#: other declaration a test/caller collects for this guard.
_GUARD_NAME = "block-dev-repo-sentinel-removal-advisory"

#: PowerShell-only verb spellings this guard's Rule 1 (`_remove_arg_denies`)
#: must ALSO recognize under `Dialect.POWERSHELL`, per
#: `docs/reference/guard-dialect-coverage.md` row 26: `rm` already fires
#: unaided because it is a real PowerShell alias (C3's alias-collision
#: finding) -- `_REMOVE_ARG_COMMANDS` below is unchanged and still matches
#: it once the tokens themselves are dialect-correct. `unlink` has NO
#: PowerShell equivalent at all, so this set names the cmdlet/alias
#: spellings that actually remove a file by positional argument in
#: PowerShell -- `Remove-Item` (full cmdlet name), `ri`/`rd`/`del` (its
#: built-in aliases). Compared case-insensitively, same as
#: `_REMOVE_ARG_COMMANDS`, via `_normalize_executable_basename`'s own
#: lower-casing.
_REMOVE_ARG_COMMANDS_POWERSHELL = frozenset({"remove-item", "ri", "rd", "del"})

#: Reason-kind for the one `_tokenize_full_command` failure this module treats
#: as DENY rather than ADVISORY: the command is past the shared tokenizer's
#: DoS ceiling (`_command_tokenizer._MAX_TOKENIZABLE_COMMAND_CHARS`) AND names
#: the sentinel filename in its raw text.
#:
#: The two failure causes are NOT symmetric and this module deliberately
#: separates them. An unterminated quote is an ordinary typo shape that a
#: person hits by accident, and demoting it to ADVISORY is this module's
#: stated posture (see "POSTURE" in the module docstring) -- that behavior is
#: unchanged. Being past the ceiling is not a typo: the largest command-shaped
#: string this project has ever needed to classify is ~8 KB, so a command
#: eight times that size which also names the sentinel is not a shape any
#: honest caller produces, and leaving it at ADVISORY (which renders as
#: `permissionDecision: allow`) would mean a padded `rm <sentinel>` walks
#: through the guard the padding was added to defeat.
#:
#: Scoped to the OVER-CEILING cause on purpose: every verdict below the
#: ceiling is bit-identical to what this module returned before the ceiling
#: existed.
#: A ``%s`` TEMPLATE, not a finished string -- interpolate
#: ``self.target_basename`` at every use, the way every other reason in this
#: module does. A static "naming the sentinel" dropped the one fact the deny
#: text exists to carry: WHICH sentinel.
_REASON_OVER_CEILING = "command past the tokenizer size ceiling, naming %s"

#: Commands that remove a named file via a plain positional argument.
_REMOVE_ARG_COMMANDS = frozenset({"rm", "unlink"})

#: Verbs, as Python identifiers, that a `python -c` payload calls to
#: actually remove/relocate a file -- as opposed to merely reading its
#: presence (`exists`, `is_file`, `stat`), which must never deny (see module
#: docstring -- `resolve_coordinator_clone.py` and `claude_md_budget.py`
#: both legitimately probe for this sentinel's presence).
_REMOVAL_VERB_RE = re.compile(r"\b(remove|unlink|rmtree|move|rename|replace)\b", re.IGNORECASE)

#: Python interpreter basenames this guard treats as `-c`-capable.
_PYTHON_BASENAME_RE = re.compile(r"^python[0-9.]*$")

#: `git` global options that consume a following token as their own
#: argument (space-separated form only) -- narrow, closed set; matches the
#: shapes named in the dispatch brief (`git -C <dir> rm ...`). An unknown
#: `-`/`--`-prefixed flag is treated as taking NO argument here (unlike the
#: destructive-git classifier's own ambiguity handling), consistent with
#: this module's advisory-on-ambiguity posture -- worst case a misresolved
#: subcommand token costs a missed deny, not a wrongly blocked command.
_GIT_VALUE_FLAGS = frozenset({"-C", "-c"})


def _basename(token: str) -> str:
    """Return `token`'s final path component, splitting on both `/` and
    `\\` (a Windows path may appear in a command string on any host
    platform)."""
    base = token.rstrip("/\\")
    base = base.rsplit("/", 1)[-1]
    base = base.rsplit("\\", 1)[-1]
    return base


class SentinelRemovalDetector:
    """Detects shell shapes that would remove or relocate-away a single
    named sentinel file. One instance per protected target basename.
    """

    def __init__(self, target_basename: str) -> None:
        self.target_basename = target_basename
        # Case-folded, same reasoning as the sibling creation detector's own
        # `_is_target`/`_mention_re` -- this fleet's primary hazard
        # filesystem (macOS APFS) is case-insensitive-but-case-preserving.
        self._mention_re = re.compile(re.escape(target_basename), re.IGNORECASE)

    def _is_target(self, token: str) -> bool:
        return _basename(token).lower() == self.target_basename.lower()

    #: Passthrough wrapper binaries that run their remaining argv unchanged
    #: -- same set the sibling creation detector tolerates.
    _PASSTHROUGH_WRAPPERS = frozenset(
        {
            "sudo", "command", "time", "exec", "nice", "nohup", "ionice", "timeout",
            "stdbuf", "which", "type", "setsid", "strace", "doas", "busybox",
        }
    )

    @staticmethod
    def _env_skip_index(seg_tokens: List[str]) -> int:
        """Return the index of the first token in `seg_tokens` that is NOT
        a leading `VAR=value` environment assignment, an `env` invocation,
        a no-op passthrough wrapper (plus its own argv), or a `{`/`(`
        grouping token -- mirrors `SentinelCreationDetector._env_skip_index`
        exactly (see that method's own docstring for the per-shape
        rationale); a free function is not shared between the two modules
        because it lives as a staticmethod on the sibling's own class."""
        env_re = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
        i = 0
        n = len(seg_tokens)
        while i < n:
            if seg_tokens[i] in ("{", "("):
                i += 1
                continue
            if env_re.match(seg_tokens[i]):
                i += 1
                continue
            base = _normalize_executable_basename(seg_tokens[i])
            if base == "env":
                i += 1
                while i < n and (
                    env_re.match(seg_tokens[i]) or seg_tokens[i] in ("-i", "--ignore-environment")
                ):
                    i += 1
                continue
            if base in SentinelRemovalDetector._PASSTHROUGH_WRAPPERS:
                i += 1
                i = _skip_wrapper_own_argv(seg_tokens, i, base)
                continue
            break
        return i

    def _remove_arg_denies(
        self, seg_tokens: List[str], argv0_idx: int, dialect: Optional[Dialect] = None
    ) -> bool:
        """Rule 1 -- `rm`/`unlink` with the sentinel as ANY argument.

        Under `Dialect.POWERSHELL`, ALSO recognizes
        `_REMOVE_ARG_COMMANDS_POWERSHELL` (`Remove-Item`/`ri`/`rd`/`del`) --
        `unlink` has no PowerShell equivalent at all, so without this widened
        set a PowerShell-typed `Remove-Item .coordinator-dev-repo` would
        tokenize correctly (once the caller supplies a PowerShell-aware
        token stream) and still fall through this rule unmatched. `rm`
        itself needs no widening here -- it is a real PowerShell alias, so
        `_REMOVE_ARG_COMMANDS` already matches it once the tokens are
        dialect-correct (guard-dialect-coverage.md row 26)."""
        base = _normalize_executable_basename(seg_tokens[argv0_idx])
        if dialect is Dialect.POWERSHELL:
            # `unlink` is a bash-only spelling with no PowerShell alias --
            # it does not resolve to any real command in PowerShell, so
            # `_REMOVE_ARG_COMMANDS` (bash's `{"rm", "unlink"}`) must NOT be
            # used verbatim here. `rm` carries over unaided (real alias
            # collision); `_REMOVE_ARG_COMMANDS_POWERSHELL` names the
            # PowerShell-only spellings (guard-dialect-coverage.md row 26).
            allowed = {"rm"} | _REMOVE_ARG_COMMANDS_POWERSHELL
        else:
            allowed = _REMOVE_ARG_COMMANDS
        if base not in allowed:
            return False
        return any(self._is_target(tok) for tok in seg_tokens[argv0_idx + 1 :])

    def _mv_source_denies(self, seg_tokens: List[str], argv0_idx: int) -> bool:
        """Rule 2 -- `mv` with the sentinel as the SOURCE argument.
        Deliberately does NOT fire when the sentinel is only the
        DESTINATION -- that is a creation shape, `_sentinel_creation_
        guard`'s territory, not this module's (module docstring)."""
        base = _normalize_executable_basename(seg_tokens[argv0_idx])
        if base != "mv":
            return False
        rest = seg_tokens[argv0_idx + 1 :]
        positional = [tok for tok in rest if not tok.startswith("-")]
        if len(positional) < 2:
            return False
        sources = positional[:-1]
        return any(self._is_target(tok) for tok in sources)

    def _git_subcommand_index(self, seg_tokens: List[str], argv0_idx: int) -> Optional[int]:
        """Index of the real git SUBCOMMAND token following `git` at
        `argv0_idx`, skipping `-C <dir>`/`-c <key>=<val>` (space-separated
        form) and any other `-`/`--`-prefixed flag (treated as taking no
        argument -- see module docstring for why that is the correct
        ambiguity direction for THIS guard). Returns `None` if no such
        token exists (bare `git`)."""
        i = argv0_idx + 1
        n = len(seg_tokens)
        while i < n:
            tok = seg_tokens[i]
            if tok in _GIT_VALUE_FLAGS:
                i += 2
                continue
            if tok.startswith("-"):
                i += 1
                continue
            return i
        return None

    def _git_rm_mv_denies(self, seg_tokens: List[str], argv0_idx: int) -> bool:
        """Rule 3 -- `git rm`/`git mv` (sentinel as source), including
        `git -C <dir> rm ...`."""
        base = _normalize_executable_basename(seg_tokens[argv0_idx])
        if base != "git":
            return False
        idx = self._git_subcommand_index(seg_tokens, argv0_idx)
        if idx is None:
            return False
        subcmd = seg_tokens[idx]
        rest = seg_tokens[idx + 1 :]
        if subcmd == "rm":
            return any(self._is_target(tok) for tok in rest)
        if subcmd == "mv":
            positional = [tok for tok in rest if not tok.startswith("-")]
            if len(positional) < 2:
                return False
            sources = positional[:-1]
            return any(self._is_target(tok) for tok in sources)
        return False

    def _find_denies(self, seg_tokens: List[str], argv0_idx: int) -> bool:
        """Rule 4 -- `find ... -delete` or `find ... -exec rm` whose
        command text mentions the sentinel."""
        base = _normalize_executable_basename(seg_tokens[argv0_idx])
        if base != "find":
            return False
        rest = seg_tokens[argv0_idx + 1 :]
        has_delete = "-delete" in rest
        has_exec_rm = False
        for i, tok in enumerate(rest):
            if tok == "-exec" and i + 1 < len(rest):
                if _normalize_executable_basename(rest[i + 1]) in _REMOVE_ARG_COMMANDS:
                    has_exec_rm = True
        if not (has_delete or has_exec_rm):
            return False
        return any(self._is_target(tok) for tok in rest)

    def _python_dash_c_denies(self, seg_tokens: List[str], argv0_idx: int) -> bool:
        """Rule 5 -- `python(3|2)? -c <code>` whose payload BOTH mentions
        the sentinel filename AND calls a removal/move verb -- narrow on
        purpose: a payload that merely reads the sentinel's presence
        (`exists`, `is_file`) must never deny (module docstring)."""
        base = _normalize_executable_basename(seg_tokens[argv0_idx])
        if not _PYTHON_BASENAME_RE.match(base):
            return False
        rest = seg_tokens[argv0_idx + 1 :]
        n = len(rest)
        for i, tok in enumerate(rest):
            payload: Optional[str] = None
            if tok == "-c" and i + 1 < n:
                payload = rest[i + 1]
            elif tok.startswith("-c") and len(tok) > 2:
                payload = tok[2:]
            if payload is not None and self._mention_re.search(payload) and _REMOVAL_VERB_RE.search(payload):
                return True
        return False

    def _segment_denies(self, seg_tokens: List[str], dialect: Optional[Dialect] = None) -> bool:
        if not seg_tokens:
            return False
        argv0_idx = self._env_skip_index(seg_tokens)
        if argv0_idx >= len(seg_tokens):
            return False
        if self._remove_arg_denies(seg_tokens, argv0_idx, dialect):
            return True
        if self._mv_source_denies(seg_tokens, argv0_idx):
            return True
        if self._git_rm_mv_denies(seg_tokens, argv0_idx):
            return True
        if self._find_denies(seg_tokens, argv0_idx):
            return True
        if self._python_dash_c_denies(seg_tokens, argv0_idx):
            return True
        return False

    # -----------------------------------------------------------------
    # INDIRECTION-WRAPPER PASS -- mirrors `SentinelCreationDetector`'s own
    # pass structurally (same primitives), but every terminal "cannot
    # examine this payload" branch below resolves to ADVISORY here instead
    # of the sibling's DENY (module docstring "POSTURE"). A genuine direct
    # match found by recursing INTO an examinable payload still returns
    # `REASON_DIRECT` (deny) -- only genuinely opaque wrappers get the
    # weaker verdict.
    # -----------------------------------------------------------------

    def _classify_payload(self, payload: str, depth: int) -> Optional[Tuple[str, str]]:
        """Leaf classifier for an unwrapped indirection payload. Returns
        `(verdict, reason_kind)` or `None` if the payload is clean."""
        tokens = _tokenize_full_command(payload)
        mentions_anywhere = bool(self._mention_re.search(payload))
        if tokens is None:
            if mentions_anywhere:
                if _exceeds_tokenizable_ceiling(payload):
                    return VERDICT_DENY, _REASON_OVER_CEILING % self.target_basename
                return (
                    VERDICT_ADVISORY,
                    "unparseable indirection payload mentioning the sentinel filename",
                )
            return None
        for seg_tokens, pipe_before in _segments_from_tokens(tokens):
            if self._segment_denies(seg_tokens):
                return (
                    VERDICT_DENY,
                    "command shape that would remove or relocate %s" % self.target_basename,
                )
            verdict = self._evaluate_segment_indirection(
                seg_tokens, pipe_before, depth, mentions_anywhere
            )
            if verdict is not None:
                return verdict
        return None

    def _evaluate_segment_indirection(
        self, seg_tokens: List[str], pipe_before: bool, depth: int, mentions_anywhere: bool
    ) -> Optional[Tuple[str, str]]:
        """Detect an interpreter/env/xargs indirection shape at THIS
        segment's head and, where reliably unwrappable, recurse. Mirrors
        `SentinelCreationDetector._evaluate_segment_indirection`'s shape
        walk; every terminal "opaque" branch below returns `VERDICT_
        ADVISORY` instead of that sibling's unconditional deny -- and,
        UNLIKE the sibling (module docstring "POSTURE"), is additionally
        gated on `mentions_anywhere`: whether the FULL command/payload text
        at this recursion level contains the sentinel basename anywhere
        (computed once per level by the caller, not merely in THIS
        segment -- `echo <sentinel> | xargs rm` needs to fire even though
        the sentinel token itself sits in the segment BEFORE the pipe, not
        in the `xargs rm` segment being classified here). Without this
        gate, an everyday, wholly-unrelated command like `bash
        some-script.sh` or `cat file | bash` would surface an advisory on
        every single invocation, which is exactly the false-positive-noise
        failure mode this guard's whole posture exists to avoid (module
        docstring "POSTURE") -- an opaque wrapper with ZERO textual hint of
        the sentinel anywhere in the command gives this guard nothing
        actionable to advise about."""
        if depth > _MAX_INDIRECTION_DEPTH:
            if mentions_anywhere:
                return (VERDICT_ADVISORY, "indirection nesting too deep (advisory, unexamined)")
            return None
        if not seg_tokens:
            return None

        argv0_idx = self._env_skip_index(seg_tokens)
        if argv0_idx >= len(seg_tokens):
            return None
        working = seg_tokens[argv0_idx:]
        env_assignment_stripped = argv0_idx > 0

        was_env_wrapped = False
        if working[0] == "env":
            stripped = _strip_env_prefix(working)
            was_env_wrapped = stripped != working
            working = stripped
        if not working:
            return None

        head_base = _normalize_executable_basename(working[0])
        norm_head = _normalize_interpreter_basename(head_base)

        if norm_head == "xargs":
            if mentions_anywhere:
                return (
                    VERDICT_ADVISORY,
                    "xargs <cmd> (command assembled from stdin -- indirection wrapper, unexamined)",
                )
            return None

        if norm_head in _SHELL_FILE_INTERPRETERS and pipe_before:
            if mentions_anywhere:
                return (
                    VERDICT_ADVISORY,
                    f"{norm_head} (bare interpreter fed via stdin pipe -- "
                    "indirection wrapper, piped content unexamined)",
                )
            return None

        if norm_head in _C_FLAG_INTERPRETERS:
            if norm_head in _SHELL_FILE_INTERPRETERS and _has_noexec_flag_before_script(
                working[1:]
            ):
                return None
            c_flag_positions = [
                i for i in range(1, len(working)) if _BUNDLED_C_FLAG_RE.match(working[i])
            ]
            if c_flag_positions:
                idx = c_flag_positions[0]
                if idx + 1 < len(working):
                    inline_payload = working[idx + 1]
                else:
                    inline_payload = (
                        " ".join(shlex.quote(t) for t in working[idx + 1 :])
                        or " ".join(shlex.quote(t) for t in seg_tokens)
                    )
                verdict = self._classify_payload(inline_payload, depth + 1)
                if verdict is not None:
                    inner_verdict, inner_reason = verdict
                    return inner_verdict, f"{norm_head} -c '<inline>' -> {inner_reason}"
                return None
            if norm_head in _SHELL_FILE_INTERPRETERS and _has_script_operand(
                working[1:]
            ):
                if mentions_anywhere:
                    return (
                        VERDICT_ADVISORY,
                        f"{norm_head} <file> (interpreter-invoked script -- "
                        "indirection wrapper, script content unexamined)",
                    )
                return None
            return None

        if was_env_wrapped or env_assignment_stripped:
            remainder = " ".join(shlex.quote(t) for t in working)
            return self._classify_payload(remainder, depth + 1)

        return None

    def _segment_is_unresolved_indirection(self, seg_tokens: List[str]) -> bool:
        """`True` iff `seg_tokens`' head (after the same env/wrapper skip
        used everywhere else in this class) is one of the indirection-
        wrapper shapes the bash-leg walk (`_evaluate_segment_indirection`)
        knows how to recurse into -- `xargs`, or a `-c`-flag / bare-file
        interpreter call (`_C_FLAG_INTERPRETERS` / `_SHELL_FILE_
        INTERPRETERS`). Deliberately does NOT attempt to recurse into the
        wrapped payload itself (see `evaluate`'s POWERSHELL branch
        docstring for why that recursion stays bash-only, out of this
        cohort's scope) -- this is a SHAPE CHECK only, used solely to
        decide whether the PowerShell branch below has enough information
        to know it is looking at an indirection wrapper it cannot resolve,
        as opposed to a wholly ordinary command it has already correctly
        ruled ALLOW on.

        Deliberately narrow -- a leading `VAR=value`/`env`/passthrough-
        wrapper prefix is NOT by itself an unresolved shape: `_env_skip_
        index` (used by `_segment_denies`/`_remove_arg_denies` too) already
        walks past that prefix to the real argv0, so e.g. `env FOO=bar rm
        <sentinel>` is fully examinable and correctly denies DIRECTLY,
        never reaching this method's caller at all. Only a head this
        module cannot see PAST -- `xargs` (command assembled from stdin),
        or a shell-file/`-c`-flag interpreter, whose payload the bash walk
        would recurse into but this dialect branch does not -- counts."""
        if not seg_tokens:
            return False
        argv0_idx = self._env_skip_index(seg_tokens)
        if argv0_idx >= len(seg_tokens):
            return False
        working = seg_tokens[argv0_idx:]
        if working[0] == "env":
            working = _strip_env_prefix(working)
            if not working:
                return False
        head_base = _normalize_executable_basename(working[0])
        norm_head = _normalize_interpreter_basename(head_base)
        if norm_head == "xargs":
            return True
        if norm_head in _SHELL_FILE_INTERPRETERS or norm_head in _C_FLAG_INTERPRETERS:
            return True
        return False

    def evaluate(self, cmd: str, dialect: Optional[Dialect] = None) -> Tuple[str, str, str]:
        """Return `(verdict, reason_kind, reason_class)` for a raw command
        string. `verdict` is one of `VERDICT_DENY` / `VERDICT_ADVISORY` /
        `VERDICT_ALLOW`. `reason_class` is one of `REASON_DIRECT` /
        `REASON_INDIRECTION` / `""` (allow) -- see module docstring.

        `dialect` is the C2/C4 dialect-carry parameter (plan
        `2026-08-07-guards-reach-a-verdict-on-powershell-or-stay-silent.md`).
        `None` and `Dialect.BASH` are BOTH treated as the bash leg below --
        `None` preserves this method's pre-C4 call shape exactly (every
        existing caller/test that calls `evaluate(cmd)` with no second
        argument gets byte-identical behavior), while `Dialect.BASH` is the
        explicit spelling the guard module now passes once it carries a
        real dialect from `payload["tool_name"]`. `Dialect.POWERSHELL`
        tokenizes via `_dialect.resolve_segments_for_dialect` instead of
        this module's own `_tokenize_full_command` call -- the shlex-based
        tokenizer mangles PowerShell syntax outright (see `_dialect.py`
        module docstring). A `None` segment result on the PowerShell branch
        means `_dialect` already recorded SILENT for this call (parse
        failure, grammar gap, or absent `tree-sitter-pwsh`) -- this method
        does not re-derive an advisory guess on top of that (dispatch
        brief: "prefer SILENT to a guess"), it simply reports ALLOW (no
        content), consistent with declining to rule rather than guessing.

        NEGATIVE SPEC -- the indirection-wrapper walk
        (`_evaluate_segment_indirection`/`_classify_payload`, used on the
        bash leg below to recurse into `xargs`/piped-interpreter/`-c`-flag/
        `env`-wrapped payloads) stays BASH-ONLY by design on the
        PowerShell branch -- converting it to also parse the WRAPPED
        payload as PowerShell is out of C4f's triaged scope (`docs/
        reference/guard-dialect-coverage.md` row 26 names only Rule 1
        verb recognition and the top-level tokenizer swap). Silently
        skipping such a shape on the PowerShell branch would reproduce the
        exact false-clean this plan exists to close, though -- an
        indirection wrapper this guard cannot examine must not read as
        "cleared," so `_segment_is_unresolved_indirection` below is a
        SHAPE CHECK (not a resolution attempt): if a PowerShell segment's
        head is a wrapper shape the bash walk would have recursed into,
        this method records SILENT and reports ALLOW instead of a silent,
        contentless clean -- the "prefer SILENT to a guess" instruction in
        this cohort's own dispatch brief, applied to the one leg C4f does
        not convert."""
        cmd_norm = _strip_heredoc_bodies(cmd)
        mentions_anywhere = bool(self._mention_re.search(cmd_norm))

        if dialect is Dialect.POWERSHELL:
            segments = resolve_segments_for_dialect(cmd_norm, dialect, guard_name=_GUARD_NAME)
            if segments is None:
                return VERDICT_ALLOW, "", ""
            saw_unresolved_indirection = False
            for seg_tokens, _pipe_before in segments:
                if self._segment_denies(seg_tokens, dialect):
                    return (
                        VERDICT_DENY,
                        "command shape that would remove or relocate %s" % self.target_basename,
                        REASON_DIRECT,
                    )
                if self._segment_is_unresolved_indirection(seg_tokens):
                    saw_unresolved_indirection = True
            if saw_unresolved_indirection:
                record_silent(
                    _GUARD_NAME,
                    "PowerShell indirection wrapper (xargs/piped/-c-flag/env-wrapped "
                    "invocation) -- bash-only walk not converted for this dialect, "
                    "declined to rule rather than guess",
                )
            return VERDICT_ALLOW, "", ""

        tokens = _tokenize_full_command(cmd_norm)
        if tokens is None:
            if mentions_anywhere:
                if _exceeds_tokenizable_ceiling(cmd_norm):
                    return (
                        VERDICT_DENY,
                        _REASON_OVER_CEILING % self.target_basename,
                        REASON_INDIRECTION,
                    )
                return (
                    VERDICT_ADVISORY,
                    "unparseable shell shape mentioning the sentinel filename",
                    REASON_INDIRECTION,
                )
            return VERDICT_ALLOW, "", ""

        for seg_tokens, pipe_before in _segments_from_tokens(tokens):
            if self._segment_denies(seg_tokens):
                return (
                    VERDICT_DENY,
                    "command shape that would remove or relocate %s" % self.target_basename,
                    REASON_DIRECT,
                )
            verdict = self._evaluate_segment_indirection(
                seg_tokens, pipe_before, 0, mentions_anywhere
            )
            if verdict is not None:
                inner_verdict, reason = verdict
                return inner_verdict, reason, REASON_INDIRECTION
        return VERDICT_ALLOW, "", ""
