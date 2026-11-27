"""The named reasons a Unimatrix send is refused or a roster row cannot receive."""

REASONS = frozenset({
    "no_leg_listening",
    "transport_face_not_addressable",
    "session_gone",
    "socket_timeout",
    "far_unreachable",
    "target_mismatch",
    "mirror_stale",
})


def refuse(reason: str, error: str) -> dict:
    """The refusal answer `{"ok": False, "reason", "error"}`; an unnamed reason is a caller bug."""
    if reason not in REASONS:
        raise ValueError(f"unknown delivery reason {reason!r}; add it to REASONS")
    return {"ok": False, "reason": reason, "error": error}
