
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from coordinator_core import tracker_projection


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", required=True)
    parser.add_argument("--item-id", required=True)
    args = parser.parse_args()

    status = tracker_projection.render_status(args.item_id, repo_root=Path(args.repo))
    print(status)
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
