"""
coordinator_core.session.js_bridge_cli — subprocess CLI bridge for Node callers.

Port of the PUBLIC API surface of ``coordinator/lib/coordinator_session.js``
(DoE-claude, 192 lines): ``resolveLiveSessionIds``, ``claimPath``, ``selfClaim``.
That JS shim shelled out to ``coordinator/lib/coordinator-session.sh`` (the
bash session hub; DoE e34f2484, 2026-07-22 — since retired) via
``child_process.execFileSync('bash', ...)`` so Node writers
(e.g. ``coordinator/bin/refresh-queries.js``) could self-claim touched paths
without reimplementing the atomic-dedup-append logic. Both
``coordinator_session.js`` and its sibling ``resolve-claude-klabauter-node.js`` were
themselves deleted (retired 2026-07-27) — this docstring's description of
the JS shim is historical provenance for the port, not a live pointer.

This module is the Python-side analogue of that bridge target: instead of
shelling out to bash, a future Node caller shells out to
``python3 -m coordinator_core.session.js_bridge_cli <subcommand> ...`` and
gets the SAME behavior from the native session engine (``coordinator_core.
session.core`` / ``.liveness`` / ``.claims`` — the full T4a-g1 port, see
those modules' docstrings), instead of the retired bash lib.

THIS IS A NEW CLI ENTRYPOINT, NOT A REGISTERED OP — no ``@register_op``, not
listed in ``ops/__init__.py`` / ``_registry_map.py`` / ``ipc.py`` /
``authz/classification.py``. Node's ``child_process`` model shells out to a
process either way (bash or python), so there is no in-process-import
shortcut available to a cross-language caller the way there is for a Python
trampoline — a CLI subprocess is the correct shape here, matching the JS
shim's OWN existing subprocess-based design (it already shells out to bash
today; this replaces the target of that shell-out, not the shape).

Caller-repoint status: moot as of 2026-07-27 — coordinator_session.js was
deleted (no live caller ever repointed to this module before the JS shim
itself was retired). This module remains as the native session-engine CLI
bridge for any future cross-language caller; it does not itself rewire
anything.

Subcommands (mirror the three JS exports 1:1, with one deliberate divergence):
    live-session-ids                 → resolveLiveSessionIds(): prints one sid
                                        per line (empty output if none live).
                                        Deliberately SORTED for determinism —
                                        the JS original (and the bash
                                        cs_live_session_ids glob it shelled
                                        out to) returns UNSORTED,
                                        directory-enumeration order was never
                                        a contract; see liveness.py's
                                        live_session_ids() docstring.
    claim-path <touched_file> <entry> → claimPath(touchedFile, entry): best-
                                        effort atomic-dedup-append; never
                                        raises, exit 0 always (matches JS
                                        "NEVER throws" contract). ``entry``
                                        is folded through
                                        ``claims.canonical_claim_entry``
                                        first, and an entry still ABSOLUTE
                                        after that fold is REFUSED (stderr,
                                        no write) — see below.
    self-claim <path>                → selfClaim(writtenPath): best-effort
                                        resolve-session + claim; never raises,
                                        exit 0 always.

Negative-spec:
    - ``claim-path`` normalizes at the WRITE, and drops what it cannot
      normalize. Until 2026-08-21 this subcommand forwarded ``entry`` to
      ``claims.atomic_dedup_append`` verbatim, with no normalization of its
      own, so a Node caller holding an absolute path could write an absolute
      claim key -- one that can never dedup-match, never satisfy an ownership
      lookup, and never appear in a commit set: a claim that protects nothing
      while reading as a claim. The hole was reachable; whether anything ever
      fell through it is a separate question, answered below.

      The census that should have accompanied this fix, run 2026-08-21 after
      the fact: across every ledger in this repo, live and archived, exactly
      TWO verb-line entries are non-relative, both archived, both the same
      `\\tmp\\probe_crlf.py` test probe. `atomic_dedup_append` writes
      `scope.format_touch_event("T", entry)`, so anything it ever wrote is
      verb-prefixed -- which means it has never poisoned a live ledger. This
      is a FORWARD GUARD on a reachable hole, not a repair of a realised one,
      and saying otherwise would retire a question that is still open.

      Fixing the READERS to tolerate an absolute key is how this class of gap
      survives -- every prior compensation paid a git spawn per entry. Do NOT
      re-add a reader-side tolerance, and do NOT re-derive a normalization
      dialect here: the fold is ``claims.canonical_claim_entry``, shared with
      the reader (``claim_index._normalize_key``) and subprocess-free by
      construction.
    - A refused entry is REPORTED, never silently swallowed — a dropped claim
      is a path nobody is protecting, so the stderr line names it and names
      the alternative (``self-claim``, which relativizes and pays the spawn
      to do it). Exit stays 0: refusal is not a failure of the "never throws"
      contract, it is that contract applied to an unusable argument.
    - Do NOT re-implement claim/liveness logic here — this module is a THIN
      argv/stdout adapter over ``coordinator_core.session.{core,liveness,
      claims}``. Any behavioral fix belongs in those modules, not here.
    - Do NOT register this as an IPC op — Node's subprocess call model has no
      resident-engine JSON-RPC client; a plain CLI is the correct interface
      shape for this specific cross-language caller.
    - main() NEVER raises and NEVER exits non-zero on a resolution failure
      (lib-not-found / no-session / ambiguous-session) — it prints a stderr
      warning and returns 0, faithfully reproducing the JS module's
      "claimPath()/selfClaim() NEVER throw or cause a non-zero exit" contract
      (coordinator_session.js:13-18 docstring).

Spec backlink: DoE-claude:pln-bash-polyglot-clean-slate-full-5c71ee
Port source: DoE-claude coordinator/lib/coordinator_session.js as it stood
at port time (retired 2026-07-27; no caller ever repointed to this module
before the JS shim was deleted).
"""

from __future__ import annotations

import sys
from typing import List, Optional

from coordinator_core.session import claims
from coordinator_core.session import liveness


def _cmd_live_session_ids(_args: List[str]) -> int:
    for sid in sorted(liveness.resolve_live_session_ids()):
        print(sid)
    return 0


def _cmd_claim_path(args: List[str]) -> int:
    if len(args) != 2:
        sys.stderr.write(
            "js_bridge_cli: claim-path requires exactly 2 args: <touched_file> <entry>\n"
        )
        return 0
    touched_file, entry = args
    normalized = claims.canonical_claim_entry(entry)
    if entry and normalized is None:
        sys.stderr.write(
            f"js_bridge_cli: claim-path entry {entry!r} is absolute — not claimed; "
            f"pass a repo-relative path or use `self-claim <path>`\n"
        )
        return 0
    try:
        claims.atomic_dedup_append(touched_file, normalized or "")
    except (OSError, ValueError) as exc:
        sys.stderr.write(
            f"js_bridge_cli: atomic_dedup_append failed — skipping self-claim for {entry}: {exc}\n"
        )
    return 0


def _cmd_self_claim(args: List[str]) -> int:
    if len(args) != 1:
        sys.stderr.write("js_bridge_cli: self-claim requires exactly 1 arg: <path>\n")
        return 0
    (path,) = args
    try:
        claims.self_claim(path)
    except (OSError, ValueError) as exc:
        sys.stderr.write(
            f"js_bridge_cli: self_claim failed — skipping self-claim for {path}: {exc}\n"
        )
    return 0


_DISPATCH = {
    "live-session-ids": _cmd_live_session_ids,
    "claim-path": _cmd_claim_path,
    "self-claim": _cmd_self_claim,
}


def main(argv: Optional[List[str]] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        sys.stderr.write(
            "js_bridge_cli: usage: <live-session-ids|claim-path|self-claim> [args...]\n"
        )
        return 0
    cmd, rest = argv[0], argv[1:]
    handler = _DISPATCH.get(cmd)
    if handler is None:
        sys.stderr.write(f"js_bridge_cli: unknown subcommand {cmd!r}\n")
        return 0
    return handler(rest)


if __name__ == "__main__":
    sys.exit(main())
