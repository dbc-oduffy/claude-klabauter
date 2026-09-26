"""coordinator_core.bash_guards._sentinel_creation_guard -- shared detection
engine factored out of `block_approval_sentinel_creation.py` so a second
Bash-level "this sentinel must be un-creatable by any agent" guard does not
need to hand-copy (and independently re-verify) the same shell-shape
parsing.

WHY THIS EXISTS. `block_approval_sentinel_creation.py` protects
`.coordinator-doctrine-edit-approved` from Bash-level creation/overwrite.
`block_worktree_sentinel_creation.py` protects a second sentinel,
`.coordinator-override-worktree-guard`, from the identical class of shell
shape (redirection, `touch`/`cp`/`mv`/`install`/`ln`/`tee`, `sed -i`,
`python -c`). Both guards are "a sentinel file's mere ABSENCE gates a
capability, so its CREATION must be un-forgeable" -- the exact same
detection problem with a different target basename. Factoring it here means
a third such guard is a target-basename plus a deny-message away, not a
fresh from-scratch reimplementation of quoting/redirection/chaining
handling that would need its own from-scratch verification.

Reuses `block_subagent_destructive_action`'s shared shell-shape
tokenizer/segmenter (`_tokenize_full_command`, `_segments_from_tokens`,
`_normalize_executable_basename`) exactly as the original single-sentinel
guard did -- so leading env-var assignments, `&&`/`;`/`|` chaining, and
POSIX quoting (including adjacent-quote concatenation) are handled
identically for every sentinel this module is asked to protect.

INDIRECTION-WRAPPER HARDENING (2026-07-28 addition, closes a confirmed live
bypass -- `bash -c "touch <sentinel>"` created the sentinel successfully
prior to this fix). The base four rules above classify on LITERAL top-level
tokens (`touch`/`cp`/redirection/`sed -i`/`python -c`), so ONE level of
shell/env/xargs indirection hid the sentinel-creating payload from every
one of them, exactly the same class of gap
`block_subagent_destructive_action.py`'s own "INDIRECTION-WRAPPER
HARDENING" section (2026-07-21) fixed for the destructive-git/rm/chmod
surfaces. Rather than re-deriving that unwrap logic a second time, this
module REUSES its primitives directly -- `_strip_env_prefix`,
`_strip_heredoc_bodies`, `_normalize_interpreter_basename`,
`_has_noexec_flag_before_script`, `_has_script_operand`, `_BUNDLED_C_FLAG_RE`,
`_C_FLAG_INTERPRETERS`, `_SHELL_FILE_INTERPRETERS`,
`_MAX_INDIRECTION_DEPTH` -- and drives them with a SENTINEL-specific leaf
classifier (`SentinelCreationDetector._segment_denies`, the four base rules
plus the new `dd of=`/`conv=` rule below) instead of that module's
git/rm/chmod leaf classifier. This is genuine reuse of the tokenizer,
segmenter, quote-handling, env/interpreter-normalization, and depth-cap
machinery -- not a from-scratch reimplementation of shell-shape parsing --
while keeping each guard's own notion of "what counts as a deny" separate,
since a sentinel-creation deny and a destructive-git deny are different
questions answered over the same shell-shape substrate. A full call into
`_evaluate_wrapper_indirection`/`_unwrap_and_classify` was NOT used as-is:
those two functions hard-code the git/rm/chmod leaf checks inline (no
classifier-callback seam exists there today), so reusing them verbatim
would have meant also matching git/rm/chmod inside a sentinel-guard
indirection payload -- correct for that guard, not this one, and would have
required adding a callback seam to a heavily-audited 2000+ line module as a
side effect of this fix. Reusing the primitives it already exposes gets the
same coverage (interpreter `-c` unwrap, `env`/env-assignment stripping,
bare-file-argument outright deny, `xargs` outright deny, heredoc-body
stripping, depth cap) without that risk.

Covers, per the shapes enumerated in the 2026-07-28 dispatch brief:
  - `bash -c "..."` / `sh -c '...'` / `zsh -c '...'` (and any versioned
    python `-c`, already covered by the base rule 4) -- unwrapped via the
    shared `-c`-flag interpreter walk, payload re-run through this
    detector's OWN leaf classifier (recursive).
  - `env sh -c "..."` and `env VAR=1 <cmd>` -- `env`-prefix stripped via
    `_strip_env_prefix`, remainder re-classified.
  - bare `VAR=1 <indirection-shape>` (no literal `env` word) -- stripped via
    this module's own `_env_skip_index`, remainder re-classified when it
    differs from the original segment.
  - `xargs` in any form -- denied OUTRIGHT (the assembled command is never
    present in the command text, same posture as the sibling guard).
  - `dd of=<sentinel>` and `conv=` variants -- NEW rule 5,
    `_dd_of_arg_denies`; the base file-arg rule (rule 2) only ever checked
    BARE positional arguments, never `key=value` tokens, so `dd` was
    entirely unguarded by any prior rule.
  - heredoc-fed interpreters (`bash <<'EOF' ... EOF`) -- the heredoc BODY is
    stripped via `_strip_heredoc_bodies` before tokenization (heredoc data
    is not shell command text), leaving the residual `bash <<'EOF'` line,
    which the interpreter-shaped-file-argument branch above denies as an
    "interpreter-invoked script, content unexamined" (the body's actual
    content -- whether or not it truly mentions the sentinel -- is
    deliberately unexamined, same over-block-over-miss posture as the
    sibling guard's own file-argument shell-interpreter deny).
  - `<interp> <file>` (bare, no `-c`) for `bash`/`sh`/`zsh` -- denied
    outright, script content unexamined (same posture as the sibling
    guard's `_SHELL_FILE_INTERPRETERS` treatment; deliberately does NOT
    extend to bare `python <file>`, which is an overwhelmingly common
    benign subagent invocation with no sentinel-creation bypass shape
    behind it -- mirrors the sibling guard's own python/file carve-out).

KNOWN OPEN GAP -- BRACE EXPANSION (documented, not solved): a purely
LEXICAL matcher such as this one cannot statically evaluate shell brace
expansion, so `touch .coordinator-override-worktree-gu{a,a}rd` (which the
shell expands to a single literal `touch <target>` at runtime) is NOT
detected by any rule in this module -- the target basename never appears as
a contiguous token in the command TEXT the guard sees. Building a brace-
expansion simulator is explicitly out of scope here (same "not asking for
an airtight parser" posture as `block_subagent_destructive_action.py`'s own
memo-cited scope boundary) -- this note exists so a future reader does not
mistake this module's rule set for exhaustive.

KNOWN OPEN GAP -- PRE-EXISTING SYMLINK INDIRECTION (documented, not solved;
2026-07-30 M13/M19 review finding, evaluated and deliberately deferred): the
Write-tool leg (`_sentinel_write_guard.is_sentinel_write`) resolves its
target through `os.path.realpath`/`os.path.abspath` before the basename
compare specifically to catch a write through a link whose OWN name is not
the sentinel but which points at it. This module cannot mirror that without
a structural departure from its own design, not a one-line fix:

  - `evaluate()`/`_is_target()` take no `cwd` at all today -- every concrete
    guard built on this module (`block_approval_sentinel_creation.py`,
    `block_worktree_sentinel_creation.py`,
    `block_disarm_marker_sentinel_creation.py`) calls `evaluate(cmd)` on
    command TEXT alone. Adding real symlink resolution would mean plumbing
    `payload["cwd"]` through every one of those `check()` functions AND
    every method in this class that calls `_is_target` (`_redirect_target_
    denies`, `_file_arg_command_denies`, `_sed_inplace_denies`, `_dd_of_arg_
    denies`, plus the recursive indirection pass) -- not a mirror of the
    Write leg's single call site, a signature change across the whole
    module.
  - It would also be the ONLY filesystem-touching code in any Bash guard in
    this package -- `block_subagent_destructive_action.py`, the sibling
    module this one explicitly reuses primitives from, does no `realpath`/
    `stat` of its own anywhere; every Bash guard here is, by design, a pure
    text classifier over the command string, which is what lets it run
    side-effect-free on a hot path that fires for every Bash call and stay
    correct even when the referenced paths do not exist yet (the ordinary
    case for a sentinel-creation guard, whose whole job is denying the
    command that would bring the target into existence). A pre-existing
    symlink is the one case where the target already exists on disk, but
    building filesystem access into this module for that one case, while
    every other rule stays existence-agnostic, is a deeper design change
    than the finding's framing suggests.
  - Only a PRE-EXISTING symlink is even reachable this way: an attacker
    naming the sentinel as `ln -s`'s own source/destination argument is
    already denied by rule 2 (`_file_arg_command_denies` checks EVERY
    argument, not only the destination), so the live gap is narrower than
    "any symlink indirection" -- it is specifically a link created before
    this guard existed, or through a surface this guard does not see at
    all (e.g. directly on disk, outside any Bash call).

Not fixed here. Flagging rather than shipping a partial (and, given the
above, likely inconsistent-with-the-rest-of-the-module) filesystem-touching
change under this row's scope.

DEFAULT POSTURE ON AMBIGUITY IS DENY, DELIBERATELY ASYMMETRIC -- unchanged
from the original guard's own posture (see its module docstring). A false
negative here is a structural failure of whatever boundary the protected
sentinel gates; a false positive costs only a rephrase.

Each concrete guard built on this module owns its own registration entry,
`PRIORITY`, and deny-message text -- this module only owns detection.

REASON CLASS (2026-07-28 addition). `evaluate()` used to return a bare
`(deny, reason_kind)` pair, and every concrete guard's `_deny_reason()`
collapsed every deny down to ONE fixed message regardless of which branch
fired. That is wrong for the indirection branch specifically: a
`_segment_denies()` (direct-shape) deny means this module POSITIVELY
MATCHED a rule against the sentinel -- the "this command would create or
modify the sentinel" assertion is literally true. An
`_evaluate_segment_indirection()` (indirection-shape) deny means the
OPPOSITE -- the payload is behind an interpreter/`env`/`xargs`/heredoc
wrapper this module cannot examine, so it denies by construction, not
because anything was found. Collapsing both into the same fixed assertion
text produces a message that is false on the indirection path: a caller
who greps their own script and finds no sentinel reference is being told
"this command would create/modify the sentinel" when the guard has no
idea whether that is true. That gap cost a live cross-repo round-trip
(operator debugged a script that never touched the sentinel because the
deny message asserted it did). `evaluate()` now returns a THIRD element,
`reason_class`, one of the module-level `REASON_DIRECT` / `REASON_INDIRECTION`
/ `""` (allow) literals below, so a consumer's `_deny_reason()` can branch
on WHICH kind of deny fired and phrase each truthfully, without having to
string-sniff `reason_kind` prose to infer it.

`reason_kind` text is NOT uniformly safe to echo across the two classes --
see each branch's own text. The direct branch's `reason_kind` always
interpolates the literal target basename (`"command shape that would
create or overwrite %s"`), so it is never echoed. Most of the indirection
branch's own literal strings (`xargs ...`, `<interp> <file> ...`, `<interp>
fed via stdin pipe ...`) name no file and are safe to surface verbatim --
but a RECURSIVE indirection verdict (`-c '<inline>' -> ...`) can still
bottom out in the direct branch's target-naming string one level down
(e.g. `bash -c "touch <sentinel>"` unwraps to exactly that). A consumer
surfacing `reason_kind` on the indirection class MUST still redact the
target basename out of it before display -- `reason_class` tells you which
branch fired, it does not by itself guarantee the string is echo-safe.
"""

from __future__ import annotations

import re
import shlex
from typing import List, Optional, Tuple

REASON_DIRECT = "direct"
REASON_INDIRECTION = "indirection"

INDIRECTION_REMEDY = (
    # Opens `_advisory_dedupe._CUE_WINDOW_RE`'s cue window deliberately:
    "Use instead: `./path/to/script.sh` -- invoke the script, do not name "
    "an interpreter (`bash path/to/script.sh` is what denies). For a "
    "non-script payload, run its steps directly, not through an "
    "interpreter/stdin/xargs wrapper, or ask the EM/PM to run it."
)

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
)
from coordinator_core.bash_guards import _dialect
from coordinator_core.bash_guards._verdict import record_silent

_REDIR_PREFIX_RE = re.compile(r"^\d*>{1,2}(?:&\d*)?")

_FILE_ARG_COMMANDS = frozenset({"touch", "cp", "mv", "install", "ln", "tee"})

_FILE_ARG_COMMANDS_POWERSHELL = frozenset(
    {"new-item", "copy-item", "move-item", "set-content", "add-content"}
)

_SED_INPLACE_RE = re.compile(r"^(?:-i|--in-place)")

_PYTHON_BASENAME_RE = re.compile(r"^python[0-9.]*$")

#: form -- unlike `_FILE_ARG_COMMANDS` above, there is no bare positional
_DD_OF_RE = re.compile(r"^of=(.+)$")


def _basename(token: str) -> str:
    base = token.rstrip("/\\")
    base = base.rsplit("/", 1)[-1]
    base = base.rsplit("\\", 1)[-1]
    return base


class SentinelCreationDetector:

    def __init__(self, target_basename: str) -> None:
        self.target_basename = target_basename
        # Case-INSENSITIVE (2026-07-30, H4 fix, same reasoning as
        # (`.COORDINATOR-BASH-GUARDS-DISARMED`) still creates a file the
        self._mention_re = re.compile(re.escape(target_basename), re.IGNORECASE)

    def _is_target(self, token: str) -> bool:
        """Case-FOLDED basename compare (2026-07-30, H4 fix). The read side
        this guard exists to protect (`_blanket_disarm`'s marker-file
        `path.exists()` check, and the sibling doctrine/worktree sentinel
        readers) is effectively case-insensitive on this fleet's primary
        hazard filesystem (macOS APFS, default case-insensitive-but-case-
        preserving): `touch .COORDINATOR-BASH-GUARDS-DISARMED` creates a
        file the reader finds, even though its basename differs from
        `self.target_basename` under a case-SENSITIVE compare. A
        case-sensitive check here silently let that create through while the
        read side still honored it. Mirrors
        `_sentinel_write_guard.is_sentinel_write`'s own case-fold, which
        exists for the identical reason (see that function's docstring) --
        this guard and the Write-tool leg must agree, not diverge, on what
        counts as the sentinel."""
        return _basename(token).lower() == self.target_basename.lower()

    def _redirect_target_denies(self, seg_tokens: List[str]) -> bool:
        n = len(seg_tokens)
        for i, tok in enumerate(seg_tokens):
            m = _REDIR_PREFIX_RE.match(tok)
            if not m:
                continue
            remainder = tok[m.end() :]
            if remainder:
                candidate = remainder
            elif i + 1 < n:
                candidate = seg_tokens[i + 1]
            else:
                continue
            if self._is_target(candidate):
                return True
        return False

    #: landed on `nice` (not a `_FILE_ARG_COMMANDS` member) and allowed a
    #: `dispatch_checks.py`'s `_BYPASS_PREFIX` already tolerates.
    _PASSTHROUGH_WRAPPERS = frozenset(
        {
            "sudo", "command", "time", "exec", "nice", "nohup", "ionice", "timeout",
            "stdbuf", "which", "type", "setsid", "strace", "doas", "busybox",
        }
    )

    #: `30`/`-c2`/`-oL` (not a `_FILE_ARG_COMMANDS` member), so the create/
    #: flag-set `dispatch_checks.py`'s `_BYPASS_WRAPPER_ARG_FLAGS` uses for

    @staticmethod
    def _env_skip_index(seg_tokens: List[str]) -> int:
        env_re = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
        i = 0
        n = len(seg_tokens)
        while i < n:
            # BRACE-GROUPING FIX (2026-07-29, code-reviewer Finding 1,
            if seg_tokens[i] == "{":
                i += 1
                continue
            # PAREN-GROUPING FIX (2026-07-29, EM-run confinement-corpus
            if seg_tokens[i] == "(":
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
            if base in SentinelCreationDetector._PASSTHROUGH_WRAPPERS:
                i += 1
                i = _skip_wrapper_own_argv(seg_tokens, i, base)
                continue
            break
        return i

    def _file_arg_command_denies(self, seg_tokens: List[str], argv0_idx: int) -> bool:
        base = _normalize_executable_basename(seg_tokens[argv0_idx])
        if base not in _FILE_ARG_COMMANDS and base not in _FILE_ARG_COMMANDS_POWERSHELL:
            return False
        return any(self._is_target(tok) for tok in seg_tokens[argv0_idx + 1 :])

    def _sed_inplace_denies(self, seg_tokens: List[str], argv0_idx: int) -> bool:
        base = _normalize_executable_basename(seg_tokens[argv0_idx])
        if base != "sed":
            return False
        rest = seg_tokens[argv0_idx + 1 :]
        has_inplace_flag = any(_SED_INPLACE_RE.match(tok) for tok in rest)
        if not has_inplace_flag:
            return False
        return any(self._is_target(tok) for tok in rest)

    def _python_dash_c_denies(self, seg_tokens: List[str], argv0_idx: int) -> bool:
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
            if payload is not None and self._mention_re.search(payload):
                return True
        return False

    def _dd_of_arg_denies(self, seg_tokens: List[str], argv0_idx: int) -> bool:
        """Rule 5 -- `dd of=<sentinel>` (any `conv=`/other `key=value`
        operands alongside it are irrelevant to the match; `dd` takes ALL
        its operands `key=value`-shaped, so this is the ONLY way `dd` can
        name an output file -- there is no bare positional form for it to
        fall into rule 2's `_FILE_ARG_COMMANDS` check)."""
        base = _normalize_executable_basename(seg_tokens[argv0_idx])
        if base != "dd":
            return False
        for tok in seg_tokens[argv0_idx + 1 :]:
            m = _DD_OF_RE.match(tok)
            if m and self._is_target(m.group(1)):
                return True
        return False

    def _segment_denies(self, seg_tokens: List[str]) -> bool:
        if not seg_tokens:
            return False
        if self._redirect_target_denies(seg_tokens):
            return True
        argv0_idx = self._env_skip_index(seg_tokens)
        if argv0_idx >= len(seg_tokens):
            return False
        if self._file_arg_command_denies(seg_tokens, argv0_idx):
            return True
        if self._sed_inplace_denies(seg_tokens, argv0_idx):
            return True
        if self._python_dash_c_denies(seg_tokens, argv0_idx):
            return True
        if self._dd_of_arg_denies(seg_tokens, argv0_idx):
            return True
        return False

    def _evaluate_legacy(self, cmd: str) -> bool:
        return bool(self._mention_re.search(cmd))

    # INDIRECTION-WRAPPER PASS (2026-07-28 addition -- see module
    # docstring "INDIRECTION-WRAPPER HARDENING"). Reuses

    def _classify_payload(self, payload: str, depth: int) -> Optional[str]:
        tokens = _tokenize_full_command(payload)
        if tokens is None:
            if self._evaluate_legacy(payload):
                return "unparseable indirection payload mentioning the sentinel filename"
            return None
        for seg_tokens, pipe_before in _segments_from_tokens(tokens):
            if self._segment_denies(seg_tokens):
                return "command shape that would create or overwrite %s" % self.target_basename
            verdict = self._evaluate_segment_indirection(seg_tokens, pipe_before, depth)
            if verdict is not None:
                return verdict
        return None

    _XARGS_READ_ONLY_HEADS = frozenset(
        {"cat", "ls", "stat", "test", "head", "tail", "wc", "file", "grep", "echo"}
    )

    #: option outside this set and `_XARGS_BARE_OPTIONS` fails closed.
    _XARGS_OPERAND_OPTIONS = frozenset(
        {
            "-a", "--arg-file", "-d", "--delimiter", "-E", "-I", "-L",
            "-n", "--max-args", "-P", "--max-procs", "-s", "--max-chars",
            "--process-slot-var",
        }
    )
    _XARGS_BARE_OPTIONS = frozenset(
        {
            "-0", "--null", "-r", "--no-run-if-empty", "-t", "--verbose",
            "-p", "--interactive", "-x", "--exit", "-o", "--open-tty",
        }
    )

    def _xargs_runs_read_only_head(self, seg_tokens: List[str]) -> bool:
        """True when this segment is `xargs [options] <head> ...` and
        `<head>` is in `_XARGS_READ_ONLY_HEADS`.

        Negative-spec: this is NOT a general `xargs` unwrap -- the command
        stdin assembles is still never seen, so only a head that cannot
        write under ANY argument list is admitted. An unrecognised option,
        a missing head, or a head that is itself a wrapper (`sh`, `env`,
        `xargs`) returns False and the parent's outright deny stands."""
        argv0_idx = self._env_skip_index(seg_tokens)
        working = seg_tokens[argv0_idx:]
        if working and working[0] == "env":
            working = _strip_env_prefix(working)
        if not working or _normalize_executable_basename(working[0]) != "xargs":
            return False
        i = 1
        while i < len(working):
            tok = working[i]
            if tok == "--":
                i += 1
                break
            if not tok.startswith("-"):
                break
            if tok in self._XARGS_OPERAND_OPTIONS:
                i += 2
                continue
            if tok in self._XARGS_BARE_OPTIONS:
                i += 1
                continue
            if tok.startswith("--") and "=" in tok:
                if tok.split("=", 1)[0] in self._XARGS_OPERAND_OPTIONS:
                    i += 1
                    continue
                return False
            if tok[:2] in {"-a", "-d", "-E", "-I", "-L", "-n", "-P", "-s", "-e", "-i", "-l"}:
                i += 1
                continue
            return False
        if i >= len(working):
            return False
        return _normalize_executable_basename(working[i]) in self._XARGS_READ_ONLY_HEADS

    def _evaluate_segment_indirection(
        self, seg_tokens: List[str], pipe_before: bool, depth: int
    ):
        if self._xargs_runs_read_only_head(seg_tokens):
            return None
        return self._evaluate_segment_indirection_walk(seg_tokens, pipe_before, depth)

    def _evaluate_segment_indirection_walk(
        self, seg_tokens: List[str], pipe_before: bool, depth: int
    ) -> Optional[str]:
        if depth > _MAX_INDIRECTION_DEPTH:
            return "indirection nesting too deep (fails closed)"
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
            return "xargs <cmd> (command assembled from stdin -- indirection wrapper)"

        if norm_head in _SHELL_FILE_INTERPRETERS and pipe_before:
            return (
                f"{norm_head} (bare interpreter fed via stdin pipe -- "
                "indirection wrapper, piped content unexamined)"
            )

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
                    return f"{norm_head} -c '<inline>' -> {verdict}"
                return None
            if norm_head in _SHELL_FILE_INTERPRETERS and _has_script_operand(
                working[1:]
            ):
                return (
                    f"{norm_head} <file> (interpreter-invoked script -- "
                    "indirection wrapper, script content unexamined)"
                )
            return None

        if was_env_wrapped or env_assignment_stripped:
            remainder = " ".join(shlex.quote(t) for t in working)
            return self._classify_payload(remainder, depth + 1)

        return None

    def evaluate(self, cmd: str) -> Tuple[bool, str, str]:
        """Return `(deny, reason_kind, reason_class)` for a raw command
        string. `reason_class` is one of `REASON_DIRECT` / `REASON_
        INDIRECTION` / `""` (allow) -- see module docstring "REASON CLASS"."""
        cmd_norm = _strip_heredoc_bodies(cmd)

        tokens = _tokenize_full_command(cmd_norm)
        if tokens is None:
            if self._evaluate_legacy(cmd_norm):
                return (
                    True,
                    "unparseable shell shape mentioning the sentinel filename",
                    REASON_DIRECT,
                )
            return False, "", ""

        for seg_tokens, pipe_before in _segments_from_tokens(tokens):
            if self._segment_denies(seg_tokens):
                return (
                    True,
                    "command shape that would create or overwrite %s" % self.target_basename,
                    REASON_DIRECT,
                )
            verdict = self._evaluate_segment_indirection(seg_tokens, pipe_before, 0)
            if verdict is not None:
                return True, verdict, REASON_INDIRECTION
        return False, "", ""

    def evaluate_for_dialect(
        self, cmd: str, dialect: Optional["_dialect.Dialect"], guard_name: str
    ) -> Tuple[bool, str, str]:
        """Dialect-aware entry point (C4e, 2026-08-07). `Dialect.BASH`
        delegates UNCHANGED to `evaluate()` above -- byte-identical bash-leg
        behavior, AC4. `Dialect.POWERSHELL` tokenizes/segments via
        `_dialect.resolve_segments_for_dialect` (the C2 seam) instead of the
        bash-only `_tokenize_full_command`/`_segments_from_tokens` pair, and
        runs each segment through `_segment_denies` (polymorphic -- a
        subclass override such as `block_approval_sentinel_creation
        ._ApprovalSentinelDetector`'s default-deny posture still applies).

        Deliberately NARROWER than `evaluate()` on the PowerShell leg: no
        indirection-wrapper pass (`_evaluate_segment_indirection` /
        `_classify_payload`) and no `_evaluate_legacy` free-text fallback --
        neither has been verified against PowerShell shapes (`Invoke-
        Expression`, `&`-call of a variable holding a script block, etc.),
        and this module's own posture (see plan body) is "prefer SILENT to a
        guess wherever PowerShell semantics are unclear." A PowerShell
        indirection-wrapper bypass is a documented gap, not silently
        claimed as covered.

        A tainted-variable-carrying subclass (`_ApprovalSentinelDetector`)
        computes its taint set from BASH-shaped tokenization inside its own
        `evaluate()` override, which this method bypasses entirely on the
        PowerShell leg -- so `_tainted_vars` is reset to empty here rather
        than left holding stale state from a PRIOR bash-dialect call on the
        same long-lived detector instance (module-level singletons are
        reused across calls, see e.g. `block_approval_sentinel_creation
        ._detector`).
        """
        if hasattr(self, "_tainted_vars"):
            self._tainted_vars = set()  # type: ignore[attr-defined]

        if dialect is _dialect.Dialect.BASH:
            return self.evaluate(cmd)

        if dialect is not _dialect.Dialect.POWERSHELL:
            record_silent(guard_name, "no recognized dialect (dialect=%r)" % (dialect,))
            return False, "", ""

        segments = _dialect.resolve_segments_for_dialect(cmd, dialect, guard_name=guard_name)
        if segments is None:
            return False, "", ""

        for seg_tokens, _pipe_before in segments:
            if self._segment_denies(seg_tokens):
                return (
                    True,
                    "command shape that would create or overwrite %s" % self.target_basename,
                    REASON_DIRECT,
                )
        return False, "", ""
