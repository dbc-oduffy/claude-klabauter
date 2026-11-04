"""agent_pipe.post -- append one agy-originated message to a Claude session's mailbox.

agy runs this as a shell command at its auto privilege:
`python -m coordinator_core.agent_pipe.post --to <session> --from-conversation <conv> <text|->`.
The line carries sender_agent antigravity and origin agent-initiated. Imports contract and mailbox only (cold-start budget).

Roster addressing: `post --from <name> --to <name> <text|->` queues a bridge message (the bridge resolves
both names at dispatch time). `bridge` is imported only on that path, never at module load.
"""
from __future__ import annotations

import argparse
import sys
import uuid
from datetime import datetime, timezone
from typing import Optional

from coordinator_core.agent_pipe import mailbox
from coordinator_core.agent_pipe.contract import ORIGIN_AGENT_INITIATED, PipeMessage, mailbox_path


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="agent_pipe.post", description=__doc__)
    ap.add_argument("--to", required=True, metavar="SESSION_ID|NAME")
    ap.add_argument("--from-conversation", metavar="CONVERSATION")
    ap.add_argument("--from", dest="from_name", metavar="NAME", help="roster identity; routes through the bridge")
    ap.add_argument("text", help="message text, or - to read stdin")
    args = ap.parse_args(argv)
    if args.from_name is None and not args.from_conversation:
        ap.error("one of --from-conversation or --from is required")
    content = sys.stdin.read() if args.text == "-" else args.text
    if args.from_name is not None:
        from coordinator_core.agent_pipe import bridge

        try:
            path = bridge.enqueue(bridge.make_message(args.from_name, args.to, content))
        except ValueError as exc:
            print(f"agent_pipe.post: {exc}", file=sys.stderr)
            return 2
        print(path)
        return 0
    try:
        mailbox_path(args.to)
    except ValueError as exc:
        print(f"agent_pipe.post: {exc}", file=sys.stderr)
        return 2
    path = mailbox.append(
        PipeMessage(
            id=uuid.uuid4().hex,
            sender_agent="antigravity",
            sender_conversation=args.from_conversation,
            to_session=args.to,
            content=content,
            ts=datetime.now(timezone.utc).isoformat(),
            origin=ORIGIN_AGENT_INITIATED,
        )
    )
    print(path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
