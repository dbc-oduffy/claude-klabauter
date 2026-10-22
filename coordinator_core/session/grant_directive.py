
from __future__ import annotations

from typing import Optional, Tuple

from coordinator_core.session.grant import (
    check_tier_u_grant,
    revoke_tier_u_grant,
    write_tier_u_grant,
)
from coordinator_core.session import core as _session_core
from coordinator_core.session.suite_authority import suite_authority
from coordinator_core import timestamps as _timestamps

ARGS_INVALID = object()

EXIT_OK = 0
EXIT_FALSE = 1
EXIT_USAGE = 2


def parse_grant_args(rest: list[str]):
    """`grant <granted_by> <note> [--ceremony <name>]` — two positionals plus
    an optional flag anywhere in the remaining argv. Returns
    `(granted_by, note, ceremony)` or `ARGS_INVALID`."""
    ceremony: Optional[str] = None
    positional: list[str] = []
    i = 0
    while i < len(rest):
        if rest[i] == "--ceremony":
            if i + 1 >= len(rest):
                return ARGS_INVALID
            ceremony = rest[i + 1]
            i += 2
            continue
        positional.append(rest[i])
        i += 1
    if len(positional) != 2:
        return ARGS_INVALID
    granted_by, note = positional
    return granted_by, note, ceremony


def parse_revoke_args(rest: list[str]):
    """`revoke [--only-ceremony <name>]` — nothing (the unguarded
    PM/session-owner form) or the guarded ceremony handback. Returns the
    ceremony name, `None` for the unguarded form, or `ARGS_INVALID`.

    A malformed guard argv must NEVER fall through to the unguarded form:
    that turns a typo into the destructive call."""
    if not rest:
        return None
    if len(rest) == 2 and rest[0] == "--only-ceremony" and rest[1]:
        return rest[1]
    return ARGS_INVALID


def parse_group_em_grant_args(rest: list[str]):
    """`group-em-grant <grantee> <note> [--ttl-minutes N] [--repo <root>]` — two
    positionals plus the two flags anywhere. Returns `(grantee, note, ttl, repo)`
    (`ttl` is `None` for the default) or `ARGS_INVALID`. The issuer is never an
    argument: a third positional is a usage error, not a smuggled identity."""
    ttl: Optional[int] = None
    repo: Optional[str] = None
    positional: list[str] = []
    i = 0
    while i < len(rest):
        flag = rest[i]
        if flag in ("--ttl-minutes", "--repo"):
            if i + 1 >= len(rest):
                return ARGS_INVALID
            value = rest[i + 1]
            if flag == "--repo":
                repo = value
            else:
                try:
                    ttl = int(value)
                except ValueError:
                    return ARGS_INVALID
            i += 2
            continue
        positional.append(flag)
        i += 1
    if len(positional) != 2:
        return ARGS_INVALID
    return positional[0], positional[1], ttl, repo


def parse_group_em_revoke_args(rest: list[str]):
    """`group-em-revoke <grantee>` — exactly one positional. Returns it or `ARGS_INVALID`."""
    if len(rest) != 1 or not rest[0]:
        return ARGS_INVALID
    return rest[0]


_GROUP_EM_REMEDY = {
    "NOT_GROUP_EM": "only the live verified box Group EM issues this",
    "GROUP_EM_NOT_LIVE": "the box Group EM session is not live; re-enter /group-em",
    "ISSUER_UNRESOLVED": "calling session id unresolvable; run from inside a session",
    "GRANTEE_UNRESOLVED": "grantee must be one live session id or a unique registry name (or pass --repo)",
    "GRANTEE_NOT_LIVE": "grantee session is not live; name a live session",
    "TTL_OUT_OF_RANGE": "--ttl-minutes must be 1..720",
    "NOTE_EMPTY": "the note is the ruling, verbatim; pass it non-empty",
}


def _with_remaining(stamp) -> str:
    """A grant expiry as stored, followed by how long until it lapses.

    `timestamps.with_age` clamps a future instant to "0 seconds ago", which is
    false for an expiry; the stored bytes stay verbatim and a stamp that does
    not parse is marked rather than guessed at.
    """
    age = _timestamps.age_seconds(stamp)
    if age is None:
        return f"{stamp} ({_timestamps.UNREADABLE_AGE})"
    if age >= 0:
        return f"{stamp} (expired {_timestamps.age_phrase(age)} ago)"
    return f"{stamp} (in {_timestamps.age_phrase(-age)})"


def _run_group_em_verb(verb: str, rest: list[str]) -> Tuple[int, str]:
    from coordinator_core.group_em import suite_grant

    if verb == "group-em-grant":
        parsed = parse_group_em_grant_args(rest)
        if not isinstance(parsed, tuple):
            return EXIT_USAGE, "group-em-grant <grantee> <note> [--ttl-minutes N] [--repo <root>]"
        grantee, note, ttl, repo = parsed
        kwargs = {"repo": repo}
        if ttl is not None:
            kwargs["ttl_minutes"] = ttl
        result = suite_grant.issue_suite_grant(grantee, note, **kwargs)
        if not result.ok:
            return EXIT_FALSE, f"{result.reason}: {_GROUP_EM_REMEDY.get(result.reason, 'refused')}"
        rec = result.record or {}
        return EXIT_OK, (
            f"group-em-grant: grantee {rec.get('grantee_session_id')} "
            f"expires {_with_remaining(rec.get('expires_at'))} issuer {rec.get('issuer_session_id')}"
        )

    parsed = parse_group_em_revoke_args(rest)
    if parsed is ARGS_INVALID:
        return EXIT_USAGE, "group-em-revoke <grantee>"
    result = suite_grant.revoke_suite_grant(parsed)
    if not result.ok:
        return EXIT_FALSE, f"{result.reason}: {_GROUP_EM_REMEDY.get(result.reason, 'refused')}"
    return EXIT_OK, ""


def parse_check_args(rest: list[str]):
    """`check` — no arguments. Returns `None` on the legal empty argv, or
    `ARGS_INVALID` on anything else (mirrors the CLI's own `check`
    subcommand, which likewise rejects any `rest`)."""
    if rest:
        return ARGS_INVALID
    return None


def run_grant_directive(args: list[str], *, repo_root: Optional[str] = None) -> Tuple[int, str]:
    """Execute one `grant`/`revoke`/`check` directive in-process. Returns
    `(exit_code, message)`; `message` is diagnostic text for a non-zero code
    and empty on success — except a successful `check`, whose message is the
    one line `basis: cloud-box` or `basis: grant`. The caller decides where
    it goes (stderr/stdout for the CLI, the apply report for a ceremony).
    `check` answers suite authority (cloud-box or a live grant), not grant
    presence; it cannot see caller identity.

    `repo_root` defaults to `None`, so `merge_assemble`'s existing call
    (which never passes it) is byte-for-byte unchanged and resolves the
    session via the process cwd as before. It is threaded as `cwd` into
    EVERY verb below (`grant`, `revoke`, `check`) — not `write_tier_u_grant`
    alone — because `check_tier_u_grant`/`revoke_tier_u_grant` resolve the
    session the same way `write_tier_u_grant` does
    (`core.resolve_session_id(cwd)` / `read_tier_u_grant(cwd, ...)`); a
    grant written under a passed `repo_root` but checked against the bare
    process cwd would read UNGRANTED even though the token exists.

    Raises nothing on a business `False`: an unresolvable session id is an
    INFRA condition, reported as `EXIT_FALSE` for the caller to tolerate.
    The DR-088 layer-5 guard fails CLOSED, so an unminted grant refuses the
    Tier-U consumer rather than authorizing it — taking a whole ceremony
    down over it would be strictly worse than the prose this replaced.

    A `ValueError` from `write_tier_u_grant` (bad enum, or the
    `granted_by`/`ceremony` cross-field rule) is a CALLER defect — an
    assembler emitting a wrong shape — and returns `EXIT_USAGE`, never
    `EXIT_FALSE`: a defect must not hide inside the tolerated bucket."""
    if not args:
        return EXIT_USAGE, "grant directive: no verb"
    verb, rest = args[0], args[1:]

    if verb == "grant":
        parsed = parse_grant_args(rest)
        if not isinstance(parsed, tuple):
            return EXIT_USAGE, "grant <granted_by> <note> [--ceremony <name>]"
        granted_by, note, ceremony = parsed
        try:
            ok = write_tier_u_grant(granted_by, note, ceremony=ceremony, cwd=repo_root)
        except ValueError as exc:
            return EXIT_USAGE, f"grant: {exc}"
        return (EXIT_OK, "") if ok else (EXIT_FALSE, "grant: session id unresolvable")

    if verb in ("group-em-grant", "group-em-revoke"):
        return _run_group_em_verb(verb, rest)

    if verb == "check":
        parsed = parse_check_args(rest)
        if parsed is ARGS_INVALID:
            return EXIT_USAGE, "check"
        authority = suite_authority(repo_root)
        if authority.authorized:
            return EXIT_OK, f"basis: {authority.basis}"
        granted, record = check_tier_u_grant(repo_root) if repo_root is not None else check_tier_u_grant()
        if granted:
            return EXIT_OK, "basis: grant"
        if record is None:
            return EXIT_FALSE, "check: no Tier-U grant found for this session"
        resolved_sid = _session_core.resolve_session_id(repo_root)
        from coordinator_core.bash_guards.check_test_suite_invocation import (
            _ungranted_record_failing_gate,
        )

        why = _ungranted_record_failing_gate(record, resolved_sid, repo_root)
        return EXIT_FALSE, f"check: gate denied -- {why}"

    if verb == "revoke":
        parsed = parse_revoke_args(rest)
        if parsed is ARGS_INVALID:
            return EXIT_USAGE, "revoke [--only-ceremony <name>]"
        only_ceremony = parsed if isinstance(parsed, str) else None
        ok = revoke_tier_u_grant(repo_root, only_ceremony=only_ceremony)
        return (EXIT_OK, "") if ok else (EXIT_FALSE, "revoke: session id unresolvable")

    return EXIT_USAGE, f"grant directive: unknown verb {verb!r}"
