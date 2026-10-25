"""A test must not rewrite a tracked file after a commit without `write_seen_by_git`.

Detector: `_racy_stat_detector.find_sites`. Fix a flag with the helper, or with a
`# racy-stat-ok: <reason>` marker on the write when the test does not expect dirt.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _racy_stat_detector import find_sites, site_keys  # noqa: E402

_PRE_FIX_FIXTURE = '''
import subprocess

def _git(args, root):
    subprocess.run(["git", *args], cwd=root, check=True)

def repo(tmp_path):
    root = tmp_path
    (root / "a.py").write_text("x = 1\\n", encoding="utf-8")
    _git(["add", "."], root)
    _git(["commit", "-q", "-m", "seed"], root)
    (root / "a.py").write_text("x = 2\\n", encoding="utf-8")
    _git(["status", "--porcelain"], root)
    return root
'''


def _scan(tmp_path: Path, body: str):
    (tmp_path / "test_fixture.py").write_text(body, encoding="utf-8")
    return find_sites(tmp_path)


def test_pre_fix_shape_is_flagged(tmp_path):
    sites = _scan(tmp_path, _PRE_FIX_FIXTURE)
    assert [(s.function, s.lineno) for s in sites] == [("repo", 12)]


def test_imported_write_wrapper_after_commit_is_flagged(tmp_path):
    body = (
        "from other import _write\n"
        "def _git(*a): pass\n"
        "def fire(root):\n"
        "    _git(root, 'commit', '-q')\n"
        "    _write(root, 'a.py', 'v = 2\\n')\n"
        "    _git(root, 'status', '--porcelain')\n"
    )
    assert [s.call for s in _scan(tmp_path, body)] == ["_write"]


def test_write_after_commit_without_status_read_is_not_flagged(tmp_path):
    body = _PRE_FIX_FIXTURE.replace('    _git(["status", "--porcelain"], root)\n', "")
    assert _scan(tmp_path, body) == []


def test_status_read_before_the_write_does_not_flag(tmp_path):
    body = _PRE_FIX_FIXTURE.replace(
        '    (root / "a.py").write_text("x = 2\\n", encoding="utf-8")\n'
        '    _git(["status", "--porcelain"], root)\n',
        '    _git(["status", "--porcelain"], root)\n'
        '    (root / "a.py").write_text("x = 2\\n", encoding="utf-8")\n',
    )
    assert _scan(tmp_path, body) == []


def test_write_before_commit_is_not_flagged(tmp_path):
    body = _PRE_FIX_FIXTURE.replace(
        '    (root / "a.py").write_text("x = 2\\n", encoding="utf-8")\n', ""
    )
    assert _scan(tmp_path, body) == []


def test_helper_twin_is_not_flagged(tmp_path):
    body = _PRE_FIX_FIXTURE.replace(
        '(root / "a.py").write_text("x = 2\\n", encoding="utf-8")',
        'write_seen_by_git(root / "a.py", "x = 2\\n")',
    )
    assert _scan(tmp_path, body) == []


def test_marked_twin_is_not_flagged(tmp_path):
    body = _PRE_FIX_FIXTURE.replace(
        'encoding="utf-8")\n    _git(["status"',
        'encoding="utf-8")  # racy-stat-ok: untracked scratch file\n    _git(["status"',
    )
    assert _scan(tmp_path, body) == []


def test_reasonless_marker_is_flagged(tmp_path):
    for marker in ("# racy-stat-ok", "# racy-stat-ok:", "# racy-stat-ok:   "):
        body = _PRE_FIX_FIXTURE.replace(
            'encoding="utf-8")\n    _git(["status"', f'encoding="utf-8")  {marker}\n    _git(["status"'
        )
        sites = _scan(tmp_path, body)
        assert [s.reason for s in sites] == ["racy-stat-ok marker has no reason"], marker


_BASELINE = Path(__file__).resolve().parent / "racy_stat_baseline.txt"


def _baseline() -> set[str]:
    return {ln for ln in _BASELINE.read_text(encoding="utf-8").splitlines() if ln.strip()}


def _ratchet(root: Path, baseline: set[str]) -> tuple[set[str], set[str]]:
    """(new flagged keys absent from the baseline, baseline keys that no longer flag)."""
    keys = site_keys(root)
    return keys - baseline, baseline - keys


def test_new_bare_site_fails_the_ratchet(tmp_path):
    (tmp_path / "test_fixture.py").write_text(_PRE_FIX_FIXTURE, encoding="utf-8")
    new, stale = _ratchet(tmp_path, set())
    assert new == {"test_fixture.py::repo"} and stale == set()


def test_site_that_never_reads_status_passes_the_ratchet(tmp_path):
    body = _PRE_FIX_FIXTURE.replace('    _git(["status", "--porcelain"], root)\n', "")
    (tmp_path / "test_fixture.py").write_text(body, encoding="utf-8")
    assert _ratchet(tmp_path, set()) == (set(), set())


def test_baselined_site_passes_and_a_stale_entry_fails(tmp_path):
    (tmp_path / "test_fixture.py").write_text(_PRE_FIX_FIXTURE, encoding="utf-8")
    assert _ratchet(tmp_path, {"test_fixture.py::repo"}) == (set(), set())
    new, stale = _ratchet(tmp_path, {"test_fixture.py::repo", "test_gone.py::fixed"})
    assert new == set() and stale == {"test_gone.py::fixed"}


def test_tree_matches_the_baseline_exactly():
    root = Path(__file__).resolve().parents[1]
    new, stale = _ratchet(root, _baseline())
    assert not new, "bare post-commit write; use write_seen_by_git:\n" + "\n".join(sorted(new))
    assert not stale, "baseline entry no longer flags; delete it:\n" + "\n".join(sorted(stale))
