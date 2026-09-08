"""The frontmatter-parses check, and the false positive that would discredit it.

The load-bearing test here is `test_multi_document_record_is_not_a_finding`.
A first cut of this very check used `yaml.safe_load`, which raises ComposerError
on legal multi-document YAML, and reported 595 healthy files as broken against
38 real ones — a detector that is wrong 94% of the time does not get read twice.
If that test goes green with `safe_load_all` swapped back to `safe_load`, the
check is worthless no matter what the other tests say.

The second is `test_file_without_frontmatter_is_not_a_finding`. Demanding
frontmatter of every markdown file would be a NEW RULE this check has no mandate
to introduce; it reports malformed frontmatter, never absent frontmatter.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[3]
_SCRIPT = _REPO_ROOT / "coordinator" / "bin" / "frontmatter-parse-check.py"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("frontmatter_parse_check_unit", _SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def mod() -> ModuleType:
    return _load()


def _write(tmp_path: Path, name: str, text: str) -> Path:
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


def test_script_exists() -> None:
    assert _SCRIPT.is_file()


def test_multi_document_record_is_not_a_finding(mod, tmp_path: Path) -> None:
    """Legal multi-doc YAML. `safe_load` refuses it; `safe_load_all` does not."""
    path = _write(tmp_path, "multi.yaml", "---\na: 1\n---\nb: 2\n")
    assert mod.check(path) is None


def test_file_without_frontmatter_is_not_a_finding(mod, tmp_path: Path) -> None:
    path = _write(tmp_path, "prose.md", "# A heading\n\nBody text, no frontmatter.\n")
    assert mod.check(path) is None


def test_wellformed_frontmatter_is_not_a_finding(mod, tmp_path: Path) -> None:
    path = _write(tmp_path, "ok.md", '---\ntitle: "A record"\nstatus: open\n---\n\n# body\n')
    assert mod.check(path) is None


@pytest.mark.parametrize(
    ("name", "text"),
    [
        # A plain scalar containing ": " — the commonest class in this corpus.
        ("colon.md", "---\ntitle: Prior-Art Check: retire all bash\n---\n\n# body\n"),
        # A single-quoted scalar with an unescaped apostrophe: `plan's` ends it.
        ("apostrophe.md", "---\nnote: 'the plan's own scope'\n---\n\n# body\n"),
        # An unclosed quote.
        ("unclosed.md", '---\ntitle: "never closed\nstatus: open\n---\n\n# body\n'),
    ],
)
def test_malformed_frontmatter_is_a_finding(mod, tmp_path: Path, name: str, text: str) -> None:
    path = _write(tmp_path, name, text)
    result = mod.check(path)
    assert result is not None, f"{name} should not have parsed"
    assert "Error" in result


def test_a_finding_names_the_fixing_idiom(mod, tmp_path: Path) -> None:
    """A defect report that does not say what to do sends the reader to a wiki."""
    path = _write(tmp_path, "colon.md", "---\ntitle: Prior-Art Check: retire all bash\n---\n\n# body\n")
    assert "likely" in (mod.check(path) or "")


def test_unterminated_block_is_not_reported_as_a_parse_defect(mod, tmp_path: Path) -> None:
    """A file that opens `---` and never closes it is a truncation, a different
    class. This check refuses to report a class it cannot name precisely."""
    path = _write(tmp_path, "truncated.md", "---\ntitle: x\nstatus: open\n")
    assert mod.check(path) is None


def test_corpus_walk_reaches_records_outside_any_plausible_allowlist(mod) -> None:
    """The first cut of --corpus enumerated twelve plausible roots, exited clean
    over both repos, and had not read the directories 30 of 43 known-broken
    records were in. A green result from a checker that looked at nothing is
    worse than no checker, so the walk must reach records wherever they live.
    """
    found = {str(p.relative_to(_REPO_ROOT)).split("/")[0] for p in mod.corpus_paths(_REPO_ROOT)}
    # Directories the allowlist shape missed, all of which hold real records.
    for root in ("state", "docs", "archive"):
        assert root in found, f"corpus walk never reached {root}/"


def test_corpus_walk_skips_fixture_directories(mod, tmp_path: Path) -> None:
    """A deliberately-malformed sample is not a defect. Reporting one trains
    readers to ignore the check, which costs more than it catches."""
    (tmp_path / "fixtures").mkdir()
    bad = tmp_path / "fixtures" / "broken.md"
    bad.write_text("---\ntitle: a: b\n---\n", encoding="utf-8")
    keep = tmp_path / "real.md"
    keep.write_text('---\ntitle: "ok"\n---\n', encoding="utf-8")
    walked = set(mod.corpus_paths(tmp_path))
    assert keep in walked
    assert bad not in walked
