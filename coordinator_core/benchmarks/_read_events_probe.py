
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from coordinator_core import tracker_store


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", required=True)
    args = parser.parse_args()

    events = tracker_store.read_events(repo_root=Path(args.repo))
    print(len(events))
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
