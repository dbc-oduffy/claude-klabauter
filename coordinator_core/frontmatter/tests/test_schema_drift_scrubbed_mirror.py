"""The vendored-schema drift check passes over a scrubbed-mirror-shaped copy of every schema."""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from coordinator_core.frontmatter import schema_validate

# percolate is not in klabauter's restricted publish tree; a static import here
# fails the publish import-closure gate, so the scrub is reached at run time.
mirror_text = pytest.importorskip('coordinator_core.percolate.mirror_text').mirror_text

SCHEMAS = Path(schema_validate.__file__).resolve().parent / 'schemas'
MANIFEST = schema_validate.VENDORED_FROM_MANIFEST
REL_DIR = 'coordinator_core/frontmatter/schemas'
_MANIFEST = json.loads((SCHEMAS / MANIFEST).read_text(encoding='utf-8'))


def _blob(data: bytes) -> str:
    return hashlib.sha1(b'blob %d\0' % len(data) + data).hexdigest()  # noqa: S324


# A schema whose local bytes already differ from its recorded base is a deliberate local
# divergence: the base bytes needed to seed the DoE fixture are not on disk. Those
# schemas are NOT covered here and still report drift in the mirror.
NAMES = sorted(
    n for n, e in _MANIFEST.items() if _blob((SCHEMAS / n).read_bytes()) == e['blob']
)


def _git(cwd: Path, *args: str) -> None:
    env = {k: v for k, v in os.environ.items() if not k.startswith('GIT_')}
    env.update(
        GIT_AUTHOR_NAME='t', GIT_AUTHOR_EMAIL='t@example.invalid',
        GIT_COMMITTER_NAME='t', GIT_COMMITTER_EMAIL='t@example.invalid',
    )
    subprocess.run(
        ['git', '-c', 'core.autocrlf=false', '-c', 'commit.gpgsign=false', *args],
        cwd=cwd, env=env, check=True, capture_output=True,
    )


@pytest.fixture(scope='module')
def shapes(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, Path]:
    root = tmp_path_factory.mktemp('scrubbed_mirror')
    doe, mirror = root / 'doe', root / 'mirror'
    (doe / 'coordinator' / 'schemas').mkdir(parents=True)
    mirror.mkdir()
    for name in NAMES:
        shutil.copyfile(SCHEMAS / name, doe / 'coordinator' / 'schemas' / name)
    _git(doe, 'init', '-q')
    _git(doe, 'add', '-A')
    _git(doe, 'commit', '-q', '-m', 'doe schemas')
    shutil.copyfile(SCHEMAS / MANIFEST, mirror / MANIFEST)
    for name in NAMES:
        text = (SCHEMAS / name).read_bytes().decode('utf-8')
        (mirror / name).write_bytes(mirror_text(text, f'{REL_DIR}/{name}').encode('utf-8'))
    return doe, mirror


def test_every_scrubbed_schema_reports_no_drift(shapes: tuple[Path, Path]) -> None:
    doe, mirror = shapes
    results = schema_validate.check_schema_drift_batch([(mirror / n, 'HEAD') for n in NAMES], doe)
    drifted = {n: str(r) for n, r in zip(NAMES, results) if r is not None}
    assert drifted == {}


def test_scrub_changes_some_schema_bytes(shapes: tuple[Path, Path]) -> None:
    _doe, mirror = shapes
    changed = [n for n in NAMES if (mirror / n).read_bytes() != (SCHEMAS / n).read_bytes()]
    assert changed


def test_mirror_carries_no_codename(shapes: tuple[Path, Path]) -> None:
    _doe, mirror = shapes
    leaks = [p.name for p in mirror.iterdir() if 'claude-klabauter' in p.read_text(encoding='utf-8').lower()]
    assert leaks == []


def test_locally_edited_scrubbed_schema_still_drifts(shapes: tuple[Path, Path]) -> None:
    doe, mirror = shapes
    name = next(n for n in NAMES if (mirror / n).read_bytes() != (SCHEMAS / n).read_bytes())
    path = mirror / name
    original = path.read_bytes()
    try:
        path.write_bytes(original + b' ')
        results = schema_validate.check_schema_drift_batch([(mirror / n, 'HEAD') for n in NAMES], doe)
    finally:
        path.write_bytes(original)
    assert [n for n, r in zip(NAMES, results) if r is not None] == [name]
