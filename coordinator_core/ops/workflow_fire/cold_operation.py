"""One source-pinned engine operation for the native Workflow broker; never contacts a daemon."""
from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(__doc__)
    parser.add_argument('request', type=Path)
    parser.add_argument('response', type=Path)
    options = parser.parse_args()
    request = json.loads(options.request.read_text(encoding='utf-8'))
    from coordinator_core.ipc import dispatch_message
    reply = asyncio.run(dispatch_message({
        'jsonrpc': '2.0', 'id': 1, 'method': request['op'], 'params': request['params'],
        '_origin_worktree': request['repo_root'],
    }))
    options.response.write_text(json.dumps(reply), encoding='utf-8')


if __name__ == '__main__':
    main()
