"""Rerunnable real zero-Claude acceptance ask, confined to a new scratch Git repository."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
import uuid


def main(argv=None):
    parser = argparse.ArgumentParser(__doc__)
    parser.add_argument('--workspace', type=Path, required=True)
    parser.add_argument('--session-id', required=True)
    parser.add_argument('--caller', required=True)
    parser.add_argument('--door-image', type=Path)
    parser.add_argument('--door-provenance', type=Path)
    parser.add_argument('--shared-engine-root', type=Path)
    args = parser.parse_args(argv)
    private_engine = None
    private_values = (args.door_image, args.door_provenance, args.shared_engine_root)
    if any(value is not None for value in private_values):
        from coordinator_core.ops.workflow_fire.headless import validate_private_engine_config
        try:
            private_engine = validate_private_engine_config({
                'door_image': str(args.door_image) if args.door_image is not None else None,
                'door_provenance': str(args.door_provenance) if args.door_provenance is not None else None,
                'shared_engine_root': str(args.shared_engine_root) if args.shared_engine_root is not None else None,
            }, backend='codex')
        except ValueError as exc:
            parser.error(str(exc))
    root = args.workspace.resolve() / (datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S') + '-' + uuid.uuid4().hex[:6])
    root.mkdir(parents=True)
    from coordinator_core.git.run import run_git
    def git(*words):
        result = run_git(list(words), cwd=str(root))
        if not result.ok:
            raise subprocess.CalledProcessError(result.returncode, ['git', *words],
                                                output=result.stdout, stderr=result.stderr)
    git('init', '-q')
    (root / 'README.md').write_text('# Native Workflow acceptance\n', encoding='utf-8')
    git('add', 'README.md')
    git('commit', '-q', '-m', 'seed isolated native Workflow acceptance')
    import asyncio
    from coordinator_core.ipc import dispatch_message
    request = {
        'ask': 'Create arithmetic.py with add(a, b) returning a + b and tests/test_arithmetic.py covering add(2,3)==5. Run pytest and commit the scoped change. No publishing, pushing, installing, service changes, or writes outside this repository.',
        'ask_source': 'relayed', 'session_id': args.session_id, 'caller': args.caller,
        'cwd': str(root), 'fire': True, 'executor': 'headless', 'backend': 'codex',
    }
    if private_engine is not None:
        request['private_engine'] = private_engine
    wire = asyncio.run(dispatch_message({'jsonrpc': '2.0', 'id': 1, 'method': 'engine_ask.submit',
        'params': request, '_origin_worktree': str(root)}))
    result = wire.get('result', wire)
    (root / '.git' / 'acceptance-request.json').write_text(json.dumps(request, indent=2), encoding='utf-8')
    (root / '.git' / 'acceptance-submitted.json').write_text(json.dumps(result, indent=2), encoding='utf-8')
    print(json.dumps({'repo_root': str(root), **result}, indent=2))


if __name__ == '__main__':
    main()
