"""
coordinator_core.ops.fanout.transport — the channel seam.

Purpose: everything that knows what a channel is, keyed by the manifest's `channel` value.
Compose, census and reconcile call `for_channel(manifest["channel"])` and never inspect a
transport's shape; a new substrate is one new class and one `_TRANSPORTS` entry here.

Negative spec: pure string work — no file, process or network access.
"""

from __future__ import annotations

import re
from typing import Optional

_CHECKIN_RE = re.compile(r"\[session (?P<sid>[^\s\]]+)\] channel: (?P<url>https?://\S+)")


class PrTransport:
    """The draft-PR channel: each session owns a PR in the comms repo; a comment is its inbox."""

    name = "pr"

    @staticmethod
    def checkin_line(session_id: str, channel_url: str) -> str:
        return f"[session {session_id}] channel: {channel_url}"

    def child_prompt_block(self, manifest: dict, worker: dict) -> str:
        parent_url = manifest["parent"]["channel_pr_url"]
        return "\n".join(
            [
                "Channel protocol:",
                f"- Your parent's channel is {parent_url}.",
                f"- First act: open your own draft channel PR in {manifest['comms_repo']} "
                "(its own branch with an anchor commit) and subscribe to it.",
                "- Then post exactly this line as a comment on your parent's channel, "
                "substituting your session id and your channel PR url:",
                f"  {self.checkin_line('<your-session-id>', '<your-channel-pr-url>')}",
                "- On exit, close your channel PR and delete its branch.",
            ]
        )

    def parse_checkin(self, body: str) -> Optional[tuple[str, str]]:
        """(session_id, channel_url) from a comment that is exactly the check-in line, else None."""
        for line in body.splitlines():
            match = _CHECKIN_RE.fullmatch(line.strip())
            if match:
                return match.group("sid"), match.group("url")
        return None

    def broadcast_target(self, row: dict) -> Optional[str]:
        """Where a pause message for this census row goes; None when the child never checked in."""
        if not row.get("checked_in"):
            return None
        return row.get("channel_url")


_TRANSPORTS = {"pr": PrTransport()}


def for_channel(name: str) -> PrTransport:
    try:
        return _TRANSPORTS[name]
    except KeyError:
        raise ValueError(f"unknown fanout channel {name!r}") from None
