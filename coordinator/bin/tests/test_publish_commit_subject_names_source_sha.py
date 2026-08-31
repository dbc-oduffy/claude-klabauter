r"""Every percolate commit subject names the SOURCE commit its bytes were cut from.

The publish seam had a currency signal for neither party. A mirror consumer
executing `X:\claude-klabauter` could not tell whether a fix it had been told
was committed in `claude-klabauter` was live for it, and the publisher could not
tell that "committed" would be read as "live" (example-retrieval-repo-ue-addon-em,
2026-08-31: fix `40abe011d` stayed live as a crash for a mirror consumer whose
only available currency check was a hand-rolled grep for the fix's own source
text). The commit subject is the one publish-side report that outlives the
round and is readable from the mirror alone, so it carries the stamp.

All three legs are pinned, because they are different code paths that produce
the same mirror history: `publish.py :: _commit_published_dests` (a bare
`coordinator-publish`), `percolate-round.py :: _build_commit_subject` (the
round, which drives publish with `--no-commit` and owns its own commit), and
`percolate-mirror.py`'s whole-mirror commit leg. A stamp on only SOME publish
commits is worse than none: a consumer reading an unstamped one cannot tell
"published before the stamp existed" from "this leg never stamps".

Negative spec: a subject-shape test alone would pass against a stamp that is
silently always empty, so the degrade path is asserted SEPARATELY from the
populated path — never inferred from it.

Run: python -m pytest coordinator/bin/tests/test_publish_commit_subject_names_source_sha.py -q
"""

from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path

_BIN_DIR = Path(__file__).resolve().parent.parent

_STAMP = re.compile(r" \[source ([0-9a-f]{12})\]$")


def _load(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, _BIN_DIR / filename)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


publish = _load("publish_source_sha_under_test", "publish.py")
round_mod = _load("percolate_round_source_sha_under_test", "percolate-round.py")


def test_publish_leg_stamps_a_twelve_hex_source_sha():
    suffix = publish._source_sha_suffix()
    assert _STAMP.fullmatch(suffix), f"unstamped publish-leg suffix: {suffix!r}"


def test_round_leg_stamps_the_same_shape():
    assert round_mod._source_sha_suffix() == publish._source_sha_suffix()


def test_round_commit_subject_carries_the_stamp():
    subject = round_mod._build_commit_subject("some-row", [], [])
    assert _STAMP.search(subject), f"round subject carries no stamp: {subject!r}"
    # The stamp is appended, never a replacement: the pre-existing report is intact.
    assert subject.startswith("percolate publish: some-row")


def test_stamp_degrades_to_empty_rather_than_raising(monkeypatch):
    """An unborn/unresolvable HEAD must not block a publish.

    Asserted independently of the populated path above: together they pin that
    the empty string means "HEAD did not resolve", never "the stamp is inert".
    """
    import coordinator_core.git.git_state as git_state

    monkeypatch.setattr(git_state, "head_sha", lambda _repo: None)
    assert publish._source_sha_suffix() == ""
    assert round_mod._source_sha_suffix() == ""


def test_every_percolate_commit_subject_site_stamps():
    """No publish leg composes a subject without the stamp.

    Pinned by source inspection rather than by calling each leg: the mirror
    leg's subject is built inside an interactive commit path this test cannot
    reach without a real dest repo and a tty, and leaving it unpinned is how a
    third leg silently stops stamping.
    """
    sites = {
        "publish.py": 'f"percolate: sync {len(paths)} path(s) to {repo_root.name} ({rows})"',
        "percolate-round.py": 'f"{added} added, {modified} modified, {removed} removed{residual})"',
        "percolate-mirror.py": 'f"({len(targets)} row(s), {len(pathspec)} file(s))"',
    }
    for filename, subject_line in sites.items():
        text = (_BIN_DIR / filename).read_text(encoding="utf-8")
        assert subject_line in text, f"{filename}: subject line moved — re-pin this test"
        tail = text.split(subject_line, 1)[1]
        assert "_source_sha_suffix()" in tail[:300], (
            f"{filename}: commit subject composed without the currency stamp"
        )
