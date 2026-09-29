"""BX-16 shape 6 body: multi-probe session-facts banner rewrite.

Split out of `dispatch_checks.py` (hot-path line ratchet). Depends only on
`_rewrite_support` and the tokenizer/classifier, never on `dispatch_checks`.
"""

from __future__ import annotations

import json
import shlex
from typing import Any, Dict, List, Optional, Tuple

from coordinator_core.bash_guards._command_tokenizer import (
    segments_from_tokens_with_pipe_flag as _bt_segments_from_tokens_with_pipe_flag,
    token_matches_binary as _bt_token_matches_binary,
)
from coordinator_core.bash_guards._helpers import operator_override_note
from coordinator_core.bash_guards._rewrite_support import (
    _allow_rewrite,
    _bt_python3_invocation,
    _crlf_strip,
    _override,
)
from coordinator_core.bash_guards._shape_classifier import (
    Shape as _BT_Shape,
    classify_command as _bt_classify_command,
)


#: git-fact probe forms this rewrite recognizes and batches into ONE
#: `git status --porcelain=v2 --branch` call -- an unrecognized git
#: subcommand (e.g. `git log`, `git diff`) makes the WHOLE banner chain fall
#: through to the advisory skeleton rather than a partial/guessed rewrite.
_SESSION_FACT_GIT_BRANCH_FORMS = (
    ("rev-parse", "--abbrev-ref", "HEAD"),
    ("branch", "--show-current"),
)
_SESSION_FACT_GIT_HEAD_FORMS = (("rev-parse", "HEAD"),)
_SESSION_FACT_GIT_STATUS_FORMS = (
    ("status",),
    ("status", "--short"),
    ("status", "-s"),
    ("status", "--porcelain"),
)


def _bt_git_probe_kind(tokens: List[str]) -> Optional[Tuple[str, Optional[str]]]:
    """Classify a tokenized `git ...` segment as one of the three
    session-fact kinds this rewrite batches into a single `git status
    --porcelain=v2 --branch` invocation (branch name, HEAD sha, dirty-file
    status), or `None` for any other git subcommand -- never guessed.

    Returns `(kind, form)`. For `kind == "branch"`, `form` distinguishes
    which of the two original commands this segment was (`"revparse"` for
    `git rev-parse --abbrev-ref HEAD`, `"showcurrent"` for `git branch
    --show-current`) -- Review: code-reviewer (Finding 2) -- the two
    commands disagree on detached-HEAD output (`rev-parse --abbrev-ref`
    prints the literal `HEAD`; `--show-current` prints empty), so batching
    them into one `_branch` variable with no memory of which was asked
    silently reproduced NEITHER real command's output on detached HEAD.
    `form` is `None` for `head_sha`/`status` (no such divergence there)."""
    if not tokens or not _bt_token_matches_binary(tokens[0], "git"):
        return None
    rest = tuple(tokens[1:])
    if rest == ("rev-parse", "--abbrev-ref", "HEAD"):
        return ("branch", "revparse")
    if rest == ("branch", "--show-current"):
        return ("branch", "showcurrent")
    if rest in _SESSION_FACT_GIT_HEAD_FORMS:
        return ("head_sha", None)
    if rest in _SESSION_FACT_GIT_STATUS_FORMS:
        return ("status", None)
    return None


def _bt_probe_segment_kind(tokens: List[str]) -> Optional[Tuple[str, Optional[str]]]:
    """Classify one non-piped multi-probe-banner segment as a translatable
    session-fact probe. Returns `(kind, extra)` or `None` if this segment is
    not one of the recognized bare-invocation forms this rewrite translates
    -- an unrecognized flag/operand/extra-argument shape makes the WHOLE
    command fall through to the advisory skeleton rather than a partial or
    guessed rewrite."""
    if not tokens:
        return None
    head = tokens[0]
    rest = tokens[1:]
    if _bt_token_matches_binary(head, "pwd") and not rest:
        return ("pwd", None)
    if _bt_token_matches_binary(head, "whoami") and not rest:
        return ("whoami", None)
    if _bt_token_matches_binary(head, "date") and not rest:
        return ("date", None)
    if _bt_token_matches_binary(head, "uname") and rest in ([], ["-a"]):
        return ("uname_a" if rest == ["-a"] else "uname", None)
    if _bt_token_matches_binary(head, "echo"):
        if any(a.startswith("-") for a in rest):
            return None  # `-e`/`-n` etc. change echo's own semantics -- don't guess
        return ("echo", " ".join(rest))
    git_kind = _bt_git_probe_kind(tokens)
    if git_kind is not None:
        kind, form = git_kind
        return ("git:" + kind, form)
    return None


def check_multiprobe_banner_rewrite(
    cmd: str,
    session_id: str = "",
    payload: Optional[Dict[str, Any]] = None,
    git_root: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """BX-16 shape 6 -- the multi-probe session-facts banner: a banner-marked
    chain like ``echo "=== SESSION FACTS ==="; git rev-parse --abbrev-ref
    HEAD; git status --short; pwd; whoami; date; uname`` forks one process
    per probe to re-derive facts the harness already knows.

    Auto-rewrites to a single `python3 -c` one-liner reproducing the same
    facts in one process when every probe segment is one of
    `_bt_probe_segment_kind`'s recognized forms, batching every git fact into
    one `git status --porcelain=v2 --branch` call. Never denies.

    Emits EITHER a concrete rewrite (every segment recognized) OR None --
    never a prose-only advisory with no `updatedInput`.
    `COORDINATOR_ALLOW_MULTIPROBE_BANNER` opts out.

    Known open finding: `_bt_classify_command`'s `MULTI_PROBE_BANNER` shape
    over-triggers on chains carrying no session-fact probe (`sed -n 1,5p a.py;
    echo ===; sed -n 6,9p b.py`); this function emits nothing for those
    because no segment is recognized. Narrowing belongs in
    `_shape_classifier`.
    """
    if not cmd:
        return None
    cmd = _crlf_strip(cmd)
    if _override("COORDINATOR_ALLOW_MULTIPROBE_BANNER", payload=payload):
        return None
    classification = _bt_classify_command(cmd)
    if classification.tokens is None:
        return None
    if (
        classification.primary is None
        or classification.primary.shape is not _BT_Shape.MULTI_PROBE_BANNER
    ):
        return None

    segments = _bt_segments_from_tokens_with_pipe_flag(classification.tokens)
    kinds: List[Tuple[str, Optional[str]]] = []
    for tokens, pipe_before in segments:
        # A piped stage inside a "banner" chain is composed, not a bare fact
        # probe: treat it as unrecognized.
        kind = None if pipe_before else _bt_probe_segment_kind(tokens)
        if kind is None:
            return None
        kinds.append(kind)

    needs_git = any(k.startswith("git:") for k, _ in kinds)
    lines: List[str] = ["import os"]
    if any(k == "whoami" for k, _ in kinds):
        lines.append("import getpass")
    if any(k == "date" for k, _ in kinds):
        lines.append("import time")
    if any(k in ("uname", "uname_a") for k, _ in kinds):
        lines.append("import platform")
    if needs_git:
        # A failed `git status` (index.lock held by a peer, not a repo) must
        # never render as a clean tree: a non-zero exit is propagated, and
        # `--no-optional-locks` keeps the read lock-free.
        lines.append("import subprocess")
        lines.append("import sys")
        lines.append(
            '_gsp = subprocess.run(["git", "--no-optional-locks", "status", '
            '"--porcelain=v2", "--branch"], capture_output=True, text=True)'
        )
        lines.append("if _gsp.returncode != 0:")
        lines.append("    sys.stderr.write(_gsp.stderr or \"\")")
        lines.append("    raise SystemExit(_gsp.returncode)")
        lines.append("_gs = _gsp.stdout.splitlines()")
        lines.append("_branch = _head = None")
        lines.append("_status_lines = []")
        lines.append("for _l in _gs:")
        lines.append('    if _l.startswith("# branch.head "):')
        lines.append('        _branch = _l.split(" ", 2)[2]')
        lines.append('    elif _l.startswith("# branch.oid "):')
        lines.append('        _head = _l.split(" ", 2)[2]')
        lines.append('    elif _l.startswith("#"):')
        lines.append("        continue")
        lines.append("    else:")
        # Kind-"2" (renamed/copied) records carry an extra score field and
        # join the two paths with a TAB; each kind is split on its own
        # fixed-field count so an embedded space never fragments a path.
        lines.append('        _kind = _l.split(" ", 1)[0]')
        lines.append('        if _kind == "1":')
        lines.append('            _f = _l.split(" ", 8)')
        lines.append('            _status_lines.append(_f[1] + " " + _f[8])')
        lines.append('        elif _kind == "2":')
        lines.append('            _f = _l.split(" ", 9)')
        lines.append('            _new, _old = _f[9].split("\\t", 1)')
        lines.append(
            '            _status_lines.append(_f[1] + " " + _new + " -> " + _old)'
        )
        lines.append('        elif _kind == "?":')
        lines.append('            _status_lines.append("?? " + _l.split(" ", 1)[1])')
        lines.append('        elif _kind == "!":')
        lines.append('            _status_lines.append("!! " + _l.split(" ", 1)[1])')

    for kind, extra in kinds:
        if kind == "pwd":
            # `os.sep`, never a literal backslash: a POSIX directory name may
            # legally contain one.
            lines.append('print(os.getcwd().replace(os.sep, "/"))')
        elif kind == "whoami":
            lines.append("print(getpass.getuser())")
        elif kind == "date":
            # `%e` is not portable to the Windows CRT; the space-padded day
            # is formatted by hand, and one `localtime()` sample feeds both
            # `strftime` calls so a rollover cannot split the banner.
            lines.append("_now = time.localtime()")
            lines.append("_day = \"%2d\" % _now.tm_mday")
            lines.append(
                'print(time.strftime("%a %b ", _now) + _day + time.strftime(" %H:%M:%S %Z %Y", _now))'
            )
        elif kind == "uname":
            lines.append("print(platform.uname().system)")
        elif kind == "uname_a":
            # `.processor` is a GNU-only `uname -a` field; omitted rather
            # than diverging on BSD/macOS.
            lines.append(
                '_u = platform.uname(); print(" ".join([_u.system, _u.node, '
                "_u.release, _u.version, _u.machine]))"
            )
        elif kind == "echo":
            lines.append("print(%s)" % json.dumps(extra))
        elif kind == "git:branch":
            # Detached HEAD: `rev-parse --abbrev-ref` prints `HEAD`,
            # `branch --show-current` prints an empty line.
            if extra == "revparse":
                lines.append(
                    'print("HEAD" if _branch == "(detached)" else (_branch or ""))'
                )
            else:  # "showcurrent"
                lines.append(
                    'print("" if _branch in (None, "(detached)") else _branch)'
                )
        elif kind == "git:head_sha":
            # `(initial)` on an unborn branch maps to empty output, matching
            # `git rev-parse HEAD`'s empty stdout.
            lines.append('print("" if _head in (None, "(initial)") else _head)')
        elif kind == "git:status":
            lines.append('print("\\n".join(_status_lines))')

    script = "\n".join(lines)
    _multiprobe_note = operator_override_note(
        "COORDINATOR_ALLOW_MULTIPROBE_BANNER", payload=payload, git_root=git_root
    )
    return _allow_rewrite(
        "%s -c %s" % (_bt_python3_invocation(), shlex.quote(script)),
        (
            "Auto-rewritten: this banner re-derives facts the harness already "
            "knows, one process per probe (89%/84%/71%/49% re-derivation). "
            "One python3 process reproduces the same facts, batching every "
            "git fact into ONE status call."
        )
        + (" %s" % _multiprobe_note if _multiprobe_note else ""),
    )
