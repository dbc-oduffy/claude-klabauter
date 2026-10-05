"""Declaration semantics of the declared-pair census on synthetic tmp_path repos.

Every case builds a repo whose `.git/index` is written in process and calls
`assemble(root, cold=<in-process finder>, cache_dir=<tmp>)`, so nothing spawns.
Each assertion pins the record's exact `detail` text; the cases are the
declaration-validation half of `test_generator_provenance.py` (DR-305), with no
write-detection case: a module that declares nothing has no record.
"""

from __future__ import annotations

import hashlib
import os
import re
import struct
from pathlib import Path

import pytest

from coordinator_core.git.index_write import _build_entry
from coordinator_core.ops.generator_census import GeneratorRecord, Pair, store
from coordinator_core.ops.generator_census.assemble import assemble
from coordinator_core.ops.staleness_git import Verdict

OLD = 1_700_000_000
R = "coordinator_core/m.py"

_BASE_FILES = {
    "src.txt": "x\n",
    "state/foo.yaml": "x: 1\n",
    "state/sub/foo.yaml": "x: 1\n",
    "state/ledger.md": "# ledger\n",
    "state/audits/one.md": "x\n",
    "docs/plans/foo.md": "x\n",
}
_CANDIDATE = re.compile(
    r"^(GENERATES|MUTATES|MUTATES_APPEND|GENERATES_EXTERNAL|UNSTAMPED_BY_DESIGN)\b",
    re.MULTILINE,
)


class _Cold:
    """In-process candidate finder; records the miss set of every call."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []

    def __call__(self, repo_root, missing):
        missing = sorted(missing)
        self.calls.append(tuple(missing))
        return [
            p
            for p in missing
            if _CANDIDATE.search(Path(repo_root, p).read_bytes().decode("utf-8", "replace"))
        ]


def _write(root: Path, rel: str, data: str | bytes) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data if isinstance(data, bytes) else data.encode("utf-8"))
    os.utime(path, (OLD, OLD))


def _write_index(root: Path) -> None:
    names = sorted(
        p.relative_to(root).as_posix()
        for p in root.rglob("*")
        if p.is_file() and ".git" not in p.relative_to(root).parts
    )
    entries = b""
    for name in names:
        data = (root / name).read_bytes()
        sha = hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()
        entries += _build_entry(name.encode(), 0o100644, sha, os.stat(root / name))
    raw = b"DIRC" + struct.pack(">II", 2, len(names)) + entries + b"\x00" * 20
    index = root / ".git" / "index"
    index.write_bytes(raw)
    os.utime(index, (OLD + 1000, OLD + 1000))


def _repo(root: Path, modules: dict[str, str | bytes], extra: dict[str, str] | None = None) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / ".git").mkdir(exist_ok=True)
    for rel, text in {**_BASE_FILES, **(extra or {})}.items():
        _write(root, rel, text)
    for rel, text in modules.items():
        _write(root, rel, text)
    _write_index(root)
    return root


def _census(root: Path, cache: Path, cold: _Cold | None = None) -> dict[str, GeneratorRecord]:
    records = assemble(root, cold=cold or _Cold(), cache_dir=cache)
    return {r.generator: r for r in records}


def _one(tmp_path: Path, source: str | bytes, extra: dict[str, str] | None = None):
    root = _repo(tmp_path / "repo", {R: source}, extra)
    return _census(root, tmp_path / "cache").get(R)


_ENTRY = {"artifact": "out.json", "stamp_key": "generated_at", "sources": ["src.txt"]}
_PAIR = Pair(R, "out.json", "generated_at", ("src.txt",))
_NOT_LITERAL = "__MALFORMED__"


def _gen(**over) -> str:
    entry = {**_ENTRY, **over}
    return f"GENERATES = [{entry!r}]\n"


def _entry(**over) -> dict:
    return {**_ENTRY, **over}


def _no_artifact() -> dict:
    return {"stamp_key": "generated_at", "sources": ["src.txt"]}


def _no_stamp() -> dict:
    return {"artifact": "out.json", "sources": ["src.txt"]}


# --- GENERATES -------------------------------------------------------------

_SOURCES_MSG = (
    "{R} GENERATES entry for 'out.json' has malformed sources "
    "(empty, not a list, or naming an absent path): {sources!r}"
)


@pytest.mark.parametrize(
    "source, verdict, pairs, detail",
    [
        pytest.param(_gen(), None, (_PAIR,), f"{R} declares 1 pair(s)", id="valid-pair"),
        pytest.param(
            "GENERATES = []\n",
            None,
            (),
            f"{R} declares GENERATES = [] (declared-empty, no artifacts)",
            id="declared-empty",
        ),
        pytest.param(
            'GENERATES = "out.json"\n',
            Verdict.UNDECLARED,
            (),
            f"{R} GENERATES is not a list",
            id="non-list",
        ),
        pytest.param(
            'GENERATES = {"artifact": "out.json"}\n',
            Verdict.UNDECLARED,
            (),
            f"{R} GENERATES is not a list",
            id="mapping-not-a-list",
        ),
        pytest.param(
            'GENERATES = ["out.json"]\n',
            Verdict.UNDECLARED,
            (),
            f"{R} GENERATES entry is not a mapping: 'out.json'",
            id="non-mapping-entry",
        ),
        pytest.param(
            f"GENERATES = [{_no_artifact()!r}]\n",
            Verdict.UNDECLARED,
            (),
            f"{R} GENERATES entry missing a valid artifact: {_no_artifact()!r}",
            id="missing-artifact",
        ),
        pytest.param(
            _gen(artifact=""),
            Verdict.UNDECLARED,
            (),
            f"{R} GENERATES entry missing a valid artifact: {_entry(artifact='')!r}",
            id="empty-artifact",
        ),
        pytest.param(
            f"GENERATES = [{_no_stamp()!r}]\n",
            Verdict.UNDECLARED,
            (),
            f"{R} GENERATES entry missing a valid stamp_key: {_no_stamp()!r}",
            id="missing-stamp-key",
        ),
        pytest.param(
            _gen(stamp_key=""),
            Verdict.UNDECLARED,
            (),
            f"{R} GENERATES entry missing a valid stamp_key: {_entry(stamp_key='')!r}",
            id="empty-stamp-key",
        ),
        pytest.param(
            _gen(sources=[]),
            Verdict.UNDECLARED,
            (),
            _SOURCES_MSG.format(R=R, sources=[]),
            id="empty-sources",
        ),
        pytest.param(
            _gen(sources="src.txt"),
            Verdict.UNDECLARED,
            (),
            _SOURCES_MSG.format(R=R, sources="src.txt"),
            id="non-list-sources",
        ),
        pytest.param(
            _gen(sources=["absent.txt"]),
            Verdict.UNDECLARED,
            (),
            _SOURCES_MSG.format(R=R, sources=["absent.txt"]),
            id="absent-source-path",
        ),
        pytest.param(
            _gen(sources=["src.txt", ""]),
            Verdict.UNDECLARED,
            (),
            _SOURCES_MSG.format(R=R, sources=["src.txt", ""]),
            id="empty-string-source",
        ),
        pytest.param(
            "GENERATES = build()\n",
            Verdict.UNDECLARED,
            (),
            f"{R} has a GENERATES assignment that is not a literal list",
            id="malformed-call",
        ),
        pytest.param(
            "GENERATES = [dict(artifact='out.json')]\n",
            Verdict.UNDECLARED,
            (),
            f"{R} has a GENERATES assignment that is not a literal list",
            id="malformed-element-call",
        ),
    ],
)
def test_generates_rules(tmp_path, source, verdict, pairs, detail):
    record = _one(tmp_path, source)

    assert record is not None
    assert record.verdict == verdict
    assert record.pairs == pairs
    assert record.detail == detail
    assert record.mutates == ()


def test_generates_pairs_keep_declaration_order(tmp_path):
    second = {"artifact": "b.json", "stamp_key": "k", "sources": ["src.txt", "state/foo.yaml"]}
    record = _one(tmp_path, f"GENERATES = [{_ENTRY!r}, {second!r}]\n")

    assert record.pairs == (_PAIR, Pair(R, "b.json", "k", ("src.txt", "state/foo.yaml")))
    assert record.detail == f"{R} declares 2 pair(s)"


def test_first_bad_entry_wins_over_a_later_good_one(tmp_path):
    record = _one(tmp_path, f"GENERATES = ['x', {_ENTRY!r}]\n")

    assert record.verdict == Verdict.UNDECLARED
    assert record.pairs == ()


def test_module_declaring_nothing_has_no_record(tmp_path):
    assert _one(tmp_path, "def run():\n    open('out.json', 'w').write('{}')\n") is None


def test_write_without_declaration_is_not_detected(tmp_path):
    source = "from pathlib import Path\n\ndef run():\n    Path('state/foo.yaml').write_text('x')\n"

    assert _one(tmp_path, source) is None


# --- MUTATES ---------------------------------------------------------------

_BROAD_MSG = (
    "{R} MUTATES pattern(s) {broad!r} match the whole corpus; "
    "a pathspec needs a literal directory segment or file extension"
)
_NO_MATCH_MSG = "{R} MUTATES pattern(s) match no currently-tracked path: {patterns!r}"
_SHAPE_MSG = (
    "{R} has a MUTATES declaration that is not a non-empty list of non-empty strings: {value!r}"
)
_CONCRETE_ONE = "{R} MUTATES pattern '{p}' names a concrete path; a fixed artifact declares GENERATES"
_CONCRETE_MANY = (
    "{R} MUTATES pattern(s) {ps!r} name a concrete path; a fixed artifact declares GENERATES"
)


def _declared(patterns: list[str]) -> str:
    return f"{R} declares MUTATES = {patterns!r} (corpus mutator, no staleness contract)"


@pytest.mark.parametrize(
    "patterns",
    [
        ["state/*.yaml"],
        ["state/**/*.yaml"],
        ["**/*.py"],
        ["state/*.yaml", "docs/plans/*.md"],
        ["state/[f]oo.yaml"],
        ["state/fo?.yaml"],
    ],
)
def test_mutates_valid_is_declared(tmp_path, patterns):
    record = _one(tmp_path, f"MUTATES = {patterns!r}\n")

    assert record.verdict == Verdict.MUTATES_DECLARED
    assert record.pairs == ()
    assert record.mutates == tuple(patterns)
    assert record.detail == _declared(patterns)


@pytest.mark.parametrize(
    "value_src, value",
    [
        pytest.param('"state/**/*.yaml"', "state/**/*.yaml", id="string"),
        pytest.param("[]", [], id="empty-list"),
        pytest.param('["state/**/*.yaml", 7]', ["state/**/*.yaml", 7], id="non-string-element"),
        pytest.param('[""]', [""], id="empty-string-element"),
        pytest.param("[str(x) for x in range(1)]", _NOT_LITERAL, id="non-literal"),
    ],
)
def test_mutates_malformed_shape(tmp_path, value_src, value):
    record = _one(tmp_path, f"MUTATES = {value_src}\n")

    assert record.verdict == Verdict.UNDECLARED
    assert record.mutates == ()
    assert record.detail == _SHAPE_MSG.format(R=R, value=value)


@pytest.mark.parametrize(
    "patterns",
    [["nonexistent-dir/**/*.md"], ["docs/runtime/*.jsonl"], ["nope/absent.md"]],
)
def test_mutates_matching_nothing_tracked_is_undeclared(tmp_path, patterns):
    record = _one(tmp_path, f"MUTATES = {patterns!r}\n")

    assert record.verdict == Verdict.UNDECLARED
    assert record.mutates == ()
    assert record.detail == _NO_MATCH_MSG.format(R=R, patterns=patterns)


def test_mutates_unmatched_pattern_hides_an_untracked_gitignored_match(tmp_path):
    root = _repo(tmp_path / "repo", {R: 'MUTATES = ["docs/runtime/*.jsonl"]\n'})
    _write(root, ".gitignore", "docs/runtime/\n")
    _write(root, "docs/runtime/a.jsonl", "{}\n")

    record = _census(root, tmp_path / "cache")[R]

    assert record.verdict == Verdict.UNDECLARED


@pytest.mark.parametrize("prefix", ["state", ".coordinator-local"])
def test_mutates_unmatched_runtime_ledger_is_declared(tmp_path, prefix):
    patterns = [f"{prefix}/runtime/*.jsonl"]
    record = _one(tmp_path, f"MUTATES = {patterns!r}\n")

    assert record.verdict == Verdict.MUTATES_DECLARED
    assert record.mutates == tuple(patterns)
    assert record.detail == (
        f"{R} declares runtime-ledger: MUTATES {patterns!r} matches no tracked path "
        "(no staleness contract)"
    )


def test_mutates_unmatched_concrete_runtime_path_is_a_runtime_ledger(tmp_path):
    record = _one(tmp_path, 'MUTATES = ["state/absent.md"]\n')

    assert record.verdict == Verdict.MUTATES_DECLARED
    assert "runtime-ledger" in record.detail


def test_mutates_mixed_runtime_and_other_unmatched_stays_undeclared(tmp_path):
    patterns = ["state/runtime/*.jsonl", "docs/runtime/*.jsonl"]
    record = _one(tmp_path, f"MUTATES = {patterns!r}\n")

    assert record.verdict == Verdict.UNDECLARED
    assert record.detail == _NO_MATCH_MSG.format(R=R, patterns=patterns)


@pytest.mark.parametrize("pattern", ["*", "**", "**/*", "*/*"])
def test_mutates_catch_all_is_undeclared(tmp_path, pattern):
    record = _one(tmp_path, f"MUTATES = {[pattern]!r}\n")

    assert record.verdict == Verdict.UNDECLARED
    assert record.mutates == ()
    assert record.detail == _BROAD_MSG.format(R=R, broad=[pattern])


def test_mutates_catch_all_poisons_an_otherwise_valid_list(tmp_path):
    record = _one(tmp_path, 'MUTATES = ["state/**/*.yaml", "**/*"]\n')

    assert record.verdict == Verdict.UNDECLARED
    assert record.detail == _BROAD_MSG.format(R=R, broad=["**/*"])


def test_mutates_concrete_single_names_the_path(tmp_path):
    record = _one(tmp_path, 'MUTATES = ["state/sub/foo.yaml"]\n')

    assert record.verdict == Verdict.UNDECLARED
    assert record.mutates == ()
    assert record.detail == _CONCRETE_ONE.format(R=R, p="state/sub/foo.yaml")


def test_mutates_concrete_many_names_the_list(tmp_path):
    ps = ["state/sub/foo.yaml", "docs/plans/foo.md"]
    record = _one(tmp_path, f"MUTATES = {ps!r}\n")

    assert record.verdict == Verdict.UNDECLARED
    assert record.detail == _CONCRETE_MANY.format(R=R, ps=ps)


def test_mutates_concrete_beside_a_wildcard_names_only_the_concrete(tmp_path):
    record = _one(tmp_path, 'MUTATES = ["state/sub/foo.yaml", "state/**/*.yaml"]\n')

    assert record.verdict == Verdict.UNDECLARED
    assert record.detail == _CONCRETE_ONE.format(R=R, p="state/sub/foo.yaml")


def test_mutates_globs_are_case_sensitive_on_every_os(tmp_path):
    record = _one(tmp_path, 'MUTATES = ["STATE/*.yaml"]\n')

    assert record.verdict == Verdict.UNDECLARED
    assert record.detail == _NO_MATCH_MSG.format(R=R, patterns=["STATE/*.yaml"])


def test_mutates_star_crosses_directory_separators(tmp_path):
    record = _one(tmp_path, 'MUTATES = ["state/*.csv"]\n', {"state/deep/er/only.csv": "a\n"})

    assert record.verdict == Verdict.MUTATES_DECLARED


def test_mutates_folds_a_module_own_string_constant(tmp_path):
    record = _one(tmp_path, '_ROOT = "state"\nMUTATES = [f"{_ROOT}/*.yaml"]\n')

    assert record.verdict == Verdict.MUTATES_DECLARED
    assert record.mutates == ("state/*.yaml",)


def test_mutates_annotated_assignment_is_a_declaration(tmp_path):
    record = _one(tmp_path, 'MUTATES: list[str] = ["state/*.yaml"]\n')

    assert record.verdict == Verdict.MUTATES_DECLARED


# --- GENERATES together with MUTATES ---------------------------------------


def test_generates_and_mutates_together_both_honoured(tmp_path):
    record = _one(tmp_path, _gen() + 'MUTATES = ["state/*.yaml"]\n')

    assert record.verdict is None
    assert record.pairs == (_PAIR,)
    assert record.mutates == ("state/*.yaml",)
    assert record.detail == f"{R} declares 1 pair(s)"


def test_invalid_mutates_does_not_swallow_a_valid_generates(tmp_path):
    record = _one(tmp_path, _gen() + 'MUTATES = ["state/foo.yaml"]\n')

    assert record.verdict is None
    assert record.pairs == (_PAIR,)
    assert record.mutates == ()
    assert record.detail == (
        f"{R} declares 1 pair(s); " + _CONCRETE_ONE.format(R=R, p="state/foo.yaml")
    )


def test_invalid_mutates_beside_declared_empty_generates_is_appended(tmp_path):
    record = _one(tmp_path, 'GENERATES = []\nMUTATES = ["nope/*.md"]\n')

    assert record.verdict is None
    assert record.detail == (
        f"{R} declares GENERATES = [] (declared-empty, no artifacts); "
        + _NO_MATCH_MSG.format(R=R, patterns=["nope/*.md"])
    )


def test_mutates_without_generates_is_mutates_declared_with_no_pairs(tmp_path):
    record = _one(tmp_path, 'MUTATES = ["state/*.yaml"]\n')

    assert record.verdict == Verdict.MUTATES_DECLARED
    assert record.pairs == ()


# --- MUTATES_APPEND / GENERATES_EXTERNAL / UNSTAMPED_BY_DESIGN -------------

_APPEND_NOTE = "MUTATES_APPEND = {p!r} (append-only ledger or surgical edit)"


def test_mutates_append_concrete_only(tmp_path):
    record = _one(tmp_path, 'MUTATES_APPEND = ["state/ledger.md"]\n')

    assert record.verdict == Verdict.MUTATES_DECLARED
    assert record.mutates == ("state/ledger.md",)
    assert record.detail == (
        f"{R} declares " + _APPEND_NOTE.format(p=["state/ledger.md"]) + " (no staleness contract)"
    )


def test_mutates_append_beside_wildcard_mutates_lists_append_first(tmp_path):
    record = _one(tmp_path, 'MUTATES = ["state/*.md"]\nMUTATES_APPEND = ["state/ledger.md"]\n')

    assert record.verdict == Verdict.MUTATES_DECLARED
    assert record.mutates == ("state/ledger.md", "state/*.md")
    assert record.detail == (
        f"{R} declares MUTATES = ['state/*.md']; "
        + _APPEND_NOTE.format(p=["state/ledger.md"])
        + " (no staleness contract)"
    )


@pytest.mark.parametrize(
    "value_src, value",
    [
        ('"state/ledger.md"', "state/ledger.md"),
        ("[]", []),
        ('["state/*.md"]', ["state/*.md"]),
        ("[7]", [7]),
    ],
)
def test_malformed_mutates_append_is_undeclared(tmp_path, value_src, value):
    record = _one(tmp_path, f"MUTATES_APPEND = {value_src}\n")

    assert record.verdict == Verdict.UNDECLARED
    assert record.detail == (
        f"{R} MUTATES_APPEND must be a non-empty list of concrete (wildcard-free) paths: {value!r}"
    )


def test_generates_external_literal_true_is_declared(tmp_path):
    record = _one(tmp_path, "GENERATES_EXTERNAL = True\n")

    assert record.verdict == Verdict.MUTATES_DECLARED
    assert record.mutates == ()
    assert record.detail == (
        f"{R} declares GENERATES_EXTERNAL (destination is caller-supplied and foreign to this repo) "
        "(no staleness contract)"
    )


@pytest.mark.parametrize("value_src, value", [('"yes"', "yes"), ("1", 1), ("False", False)])
def test_generates_external_only_literal_true(tmp_path, value_src, value):
    record = _one(tmp_path, f"GENERATES_EXTERNAL = {value_src}\n")

    assert record.verdict == Verdict.UNDECLARED
    assert record.detail == f"{R} GENERATES_EXTERNAL must be the literal True: {value!r}"


def test_unstamped_by_design_exempts_matching_pairs(tmp_path):
    second = {"artifact": "state/audits/one.md", "stamp_key": "generated_at", "sources": ["src.txt"]}
    ledger = {"artifact": "state/ledger.md", "stamp_key": "generated_at", "sources": ["src.txt"]}
    record = _one(
        tmp_path,
        f"GENERATES = [{second!r}, {ledger!r}]\nUNSTAMPED_BY_DESIGN = ['state/audits/*.md']\n",
    )

    assert record.verdict is None
    assert [p.artifact for p in record.pairs] == ["state/ledger.md"]
    assert record.detail == (
        f"{R} declares 1 pair(s); UNSTAMPED_BY_DESIGN ['state/audits/*.md'] exempts "
        "['state/audits/one.md'] from staleness comparison"
    )


def test_unstamped_by_design_never_exempts_a_pair_outside_its_globs(tmp_path):
    ledger = {"artifact": "state/ledger.md", "stamp_key": "generated_at", "sources": ["src.txt"]}
    record = _one(
        tmp_path,
        f"GENERATES = [{ledger!r}]\nUNSTAMPED_BY_DESIGN = ['state/audits/*.md']\n",
    )

    assert [p.artifact for p in record.pairs] == ["state/ledger.md"]
    assert record.detail == f"{R} declares 1 pair(s)"


@pytest.mark.parametrize(
    "value_src, value", [('"state/*.md"', "state/*.md"), ("[]", []), ("[7]", [7]), ('[""]', [""])]
)
def test_malformed_unstamped_by_design_is_undeclared(tmp_path, value_src, value):
    record = _one(tmp_path, _gen() + f"UNSTAMPED_BY_DESIGN = {value_src}\n")

    assert record.verdict == Verdict.UNDECLARED
    assert record.pairs == ()
    assert record.detail == (
        f"{R} UNSTAMPED_BY_DESIGN must be a non-empty list of glob strings: {value!r}"
    )


# --- traps: in-string, indentation, line endings ---------------------------


@pytest.mark.parametrize("quote", ['"""', "'''"])
def test_column_zero_declaration_inside_a_string_is_ignored(tmp_path, quote):
    source = (
        "FIXTURE = " + quote + "\n"
        'GENERATES = [{"artifact": "out.json", "stamp_key": "k", "sources": ["src.txt"]}]\n'
        'MUTATES = ["state/*.yaml"]\n' + quote + "\n"
    )

    assert _one(tmp_path, source) is None


@pytest.mark.parametrize("quote", ['"""', "'''"])
def test_real_declaration_after_an_in_string_decoy_is_the_one_read(tmp_path, quote):
    source = (
        quote + "\nMUTATES = ['decoy/*.md']\n" + quote + "\n" + _gen()
    )
    record = _one(tmp_path, source)

    assert record.pairs == (_PAIR,)
    assert record.mutates == ()


def test_in_string_declaration_in_a_test_module_yields_no_record(tmp_path):
    rel = "coordinator_core/sub/tests/test_holder.py"
    source = 'SRC = """\nMUTATES = ["state/*.yaml"]\n"""\n'
    root = _repo(tmp_path / "repo", {rel: source})

    assert rel not in _census(root, tmp_path / "cache")


def test_indented_declaration_is_ignored(tmp_path):
    source = f"def run():\n    GENERATES = [{_ENTRY!r}]\n    MUTATES = ['state/*.yaml']\n"

    assert _one(tmp_path, source) is None


def test_crlf_and_lf_trees_give_identical_records(tmp_path):
    source = _gen() + 'MUTATES = [\n    "state/*.yaml",\n    "docs/plans/*.md",\n]\n'
    lf = _repo(tmp_path / "lf", {R: source.encode("utf-8")})
    crlf = _repo(tmp_path / "crlf", {R: source.replace("\n", "\r\n").encode("utf-8")})

    got_lf = _census(lf, tmp_path / "cache-lf")
    got_crlf = _census(crlf, tmp_path / "cache-crlf")

    assert got_lf[R].pairs == (_PAIR,)
    assert got_lf[R].mutates == ("state/*.yaml", "docs/plans/*.md")
    assert got_crlf == got_lf


def test_only_sweep_scope_modules_are_swept(tmp_path):
    root = _repo(
        tmp_path / "repo",
        {
            R: 'MUTATES = ["state/*.yaml"]\n',
            "bin/tool.py": 'MUTATES = ["state/*.yaml"]\n',
            "coordinator/bin/tool.py": 'MUTATES = ["state/*.yaml"]\n',
            "elsewhere/tool.py": 'MUTATES = ["state/*.yaml"]\n',
            "coordinator_core/data.txt": "MUTATES = ['state/*.yaml']\n",
        },
    )

    assert set(_census(root, tmp_path / "cache")) == {
        "coordinator/bin/tool.py",
        "bin/tool.py",
        R,
    }


def test_untracked_module_is_invisible(tmp_path):
    root = _repo(tmp_path / "repo", {R: 'MUTATES = ["state/*.yaml"]\n'})
    _write(root, "coordinator_core/untracked.py", 'MUTATES = ["state/*.yaml"]\n')

    assert set(_census(root, tmp_path / "cache")) == {R}


# --- traps: cache and dirty reads ------------------------------------------

_V1 = 'MUTATES = ["state/*.yaml"]\n'
_V2 = 'MUTATES = ["state/*.yaml", "docs/plans/*.md"]  # edited\n'


def test_second_run_asks_the_cold_finder_for_nothing(tmp_path):
    root = _repo(tmp_path / "repo", {R: _V1, "coordinator_core/plain.py": "x = 1\n"})
    cache = tmp_path / "cache"
    cold = _Cold()

    first = _census(root, cache, cold)
    second = _census(root, cache, cold)

    assert cold.calls[0] == tuple(sorted(("coordinator_core/plain.py", R)))
    assert cold.calls[1] == ()
    assert second == first


def test_unchanged_blob_is_served_from_cache_across_an_mtime_touch(tmp_path):
    root = _repo(tmp_path / "repo", {R: _V1})
    cache = tmp_path / "cache"
    cold = _Cold()
    first = _census(root, cache, cold)

    os.utime(root / R, (OLD + 500, OLD + 500))
    _write_index(root)
    second = _census(root, cache, cold)

    assert cold.calls[-1] == ()
    assert second == first


def test_dirty_file_is_read_from_the_worktree_and_not_stored(tmp_path):
    root = _repo(tmp_path / "repo", {R: _V1})
    cache = tmp_path / "cache"
    cold = _Cold()
    clean = _census(root, cache, cold)

    (root / R).write_text(_V2, encoding="utf-8")
    os.utime(root / R, (OLD, OLD))
    dirty = _census(root, cache, cold)

    assert dirty[R].mutates == ("state/*.yaml", "docs/plans/*.md")
    assert cold.calls[-1] == ()

    (root / R).write_text(_V1, encoding="utf-8")
    os.utime(root / R, (OLD, OLD))
    restored = _census(root, cache, cold)

    assert restored == clean
    assert restored[R].mutates == ("state/*.yaml",)


def test_dirty_file_is_never_offered_to_the_cold_finder(tmp_path):
    root = _repo(tmp_path / "repo", {R: _V1})
    (root / R).write_text(_V2, encoding="utf-8")
    os.utime(root / R, (OLD, OLD))
    cold = _Cold()

    records = _census(root, tmp_path / "cache", cold)

    assert R not in {p for call in cold.calls for p in call}
    assert records[R].mutates == ("state/*.yaml", "docs/plans/*.md")


def test_racy_entry_is_read_from_the_worktree(tmp_path):
    root = _repo(tmp_path / "repo", {R: _V1})
    os.utime(root / ".git" / "index", (OLD, OLD))
    cold = _Cold()

    records = _census(root, tmp_path / "cache", cold)

    assert R not in {p for call in cold.calls for p in call}
    assert records[R].mutates == ("state/*.yaml",)


def test_extractor_digest_change_invalidates_the_cache(tmp_path, monkeypatch):
    root = _repo(tmp_path / "repo", {R: _V1})
    cache = tmp_path / "cache"
    cold = _Cold()
    first = _census(root, cache, cold)
    assert _census(root, cache, cold) == first
    assert cold.calls[-1] == ()

    monkeypatch.setattr(store, "extractor_digest", lambda: "another-extractor")
    third = _census(root, cache, cold)

    assert R in cold.calls[-1]
    assert third == first


def test_a_corrupt_cache_file_reads_as_empty(tmp_path):
    root = _repo(tmp_path / "repo", {R: _V1})
    cache = tmp_path / "cache"
    cold = _Cold()
    first = _census(root, cache, cold)
    for path in cache.iterdir():
        path.write_bytes(b"\x00not json")

    again = _census(root, cache, cold)

    assert R in cold.calls[-1]
    assert again == first


def test_sources_existence_is_rechecked_against_a_cache_hit(tmp_path):
    root = _repo(tmp_path / "repo", {R: _gen()})
    cache = tmp_path / "cache"
    cold = _Cold()
    assert _census(root, cache, cold)[R].pairs == (_PAIR,)

    (root / "src.txt").unlink()
    _write_index(root)
    after = _census(root, cache, cold)[R]

    assert cold.calls[-1] == ()
    assert after.verdict == Verdict.UNDECLARED
    assert after.pairs == ()


def test_mutates_validity_is_rechecked_against_a_cache_hit(tmp_path):
    root = _repo(tmp_path / "repo", {R: 'MUTATES = ["state/*.yaml"]\n'})
    cache = tmp_path / "cache"
    cold = _Cold()
    assert _census(root, cache, cold)[R].verdict == Verdict.MUTATES_DECLARED

    for rel in ("state/foo.yaml", "state/sub/foo.yaml", "state/ledger.md", "state/audits/one.md"):
        (root / rel).unlink()
    _write_index(root)
    after = _census(root, cache, cold)[R]

    assert cold.calls[-1] == ()
    assert after.verdict == Verdict.MUTATES_DECLARED
    assert "runtime-ledger" in after.detail
