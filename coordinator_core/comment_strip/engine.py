"""Orchestrates per-file comment-strip planning with a mechanical no-code-change proof.

Each language module reports raw comment spans; this module applies the KEEP rules, removes
everything else, collapses resulting blank-line runs, and re-verifies the result via a
language-appropriate proof before returning a plan. Nothing here writes to disk — the CLI
applies plans when `--apply` is passed.
"""
from __future__ import annotations

import ast
import hashlib
import json
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

from coordinator_core.source_edit_gate.gate import run_gate
from coordinator_core.win_portability import no_console_creationflags

from . import lang_clike, lang_hash, lang_python
from .keep_rules import extract_marker_tokens, is_tooling_comment

_SCAN_TS_JS = Path(__file__).with_name("scan_ts.js")
_PROTECTED_PATHS_FILE = Path(__file__).with_name("protected_paths.json")

EXCLUDED_SUFFIXES = {
    ".md", ".json", ".jsonl", ".ndjson", ".lock", ".log", ".csv", ".txt",
    ".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".ico", ".bmp",
}
EXCLUDED_DIR_PARTS = {
    ".structural-index", "dist", "node_modules", "vendor", "third_party",
    "runs", "fixtures", "results",
}
EXCLUDED_PATH_SUBSTRINGS = ("src/lib/contract/",)

LANG_BY_SUFFIX = {
    ".py": "python",
    ".ts": "ts", ".tsx": "tsx", ".js": "js", ".mjs": "js", ".cjs": "js",
    ".c": "clike", ".h": "clike", ".cc": "clike", ".cpp": "clike", ".hpp": "clike", ".cxx": "clike",
    ".rs": "rust",
    ".sh": "shell", ".bash": "shell",
    ".ps1": "powershell",
    ".yml": "hash", ".yaml": "hash", ".toml": "hash", ".ini": "hash", ".cfg": "hash",
    ".css": "clike-noline",
    ".sql": "sql",
}

SKIPPED_LANGS_REPORTED: set[str] = set()


@dataclass
class RemovedComment:
    line_start: int
    line_end: int
    text: str
    reason: str  # "removed" | "kept-tooling" | "kept-reference"


@dataclass
class FileResult:
    path: str
    language: str
    changed: bool
    removed_count: int = 0
    kept_tooling_count: int = 0
    kept_reference_count: int = 0
    proof_ok: bool = True
    skipped_reason: str | None = None
    new_text: str | None = None
    sample_diff: list[tuple[str, str]] = field(default_factory=list)


_PROTECTED_PATHS_CACHE: dict[str, set[str]] | None = None

_SHA256_HEX_RE = re.compile(r"[0-9a-f]{64}")


def _validate_protected_paths_shape(raw: object) -> dict:
    """Fail closed on any shape defect: a `path -> content_hash` entry that isn't a
    well-formed 64-hex-char sha256 digest means the file cannot be trusted to mean what
    its keys claim, so raise rather than silently degrading to `{"sources": []}`
    (fail-open -- see the module's own `is_protected` invariant this file backs)."""
    if not isinstance(raw, dict) or not isinstance(raw.get("sources"), list):
        raise ValueError("protected_paths.json: top-level 'sources' must be a list")
    for source in raw["sources"]:
        if not isinstance(source, dict) or not isinstance(source.get("files"), dict):
            raise ValueError("protected_paths.json: each source needs an object 'files' map")
        for path, content_hash in source["files"].items():
            if not isinstance(path, str) or not path:
                raise ValueError(f"protected_paths.json: invalid path key {path!r}")
            if not isinstance(content_hash, str) or not _SHA256_HEX_RE.fullmatch(content_hash):
                raise ValueError(f"protected_paths.json: invalid sha256 hash for {path!r}")
    return raw


def _load_protected_paths() -> dict[str, set[str]]:
    """Flatten protected_paths.json's per-source-commit lists into path -> {content hashes}.

    Keyed by repo-relative path plus a sha256 of the file's bytes as of its restore
    commit, never by repo name/slug -- covers repos with no `origin` remote.

    Deliberately does NOT catch `OSError`/`JSONDecodeError`/shape-`ValueError` here: this
    file is the safety list for "previously broke a test" content, so a missing, corrupt,
    or malformed-shape file must fail the whole run closed (refuse to strip anything)
    rather than silently substituting an empty list and re-stripping a previously-known-
    dangerous file. Cross-repo hash-vs-source-commit verification was considered instead
    of/alongside shape validation and rejected as the primary mechanism: the sibling repos
    named in this file are not guaranteed checked out, and even when checked out are not
    guaranteed to be sitting at the exact recorded commit (verified live: of the three
    source repos in this file, only two of three commits were reachable from the sibling
    checkouts present on this box at review time) -- a test asserting cross-repo content
    match would be flaky on environment state, not on this file's own correctness."""
    global _PROTECTED_PATHS_CACHE
    if _PROTECTED_PATHS_CACHE is not None:
        return _PROTECTED_PATHS_CACHE
    raw = json.loads(_PROTECTED_PATHS_FILE.read_text(encoding="utf-8"))
    raw = _validate_protected_paths_shape(raw)
    mapping: dict[str, set[str]] = {}
    for source in raw["sources"]:
        for path, content_hash in source["files"].items():
            mapping.setdefault(path, set()).add(content_hash)
    _PROTECTED_PATHS_CACHE = mapping
    return mapping


def is_excluded_path(rel_path: str) -> bool:
    p = Path(rel_path)
    if p.suffix.lower() in EXCLUDED_SUFFIXES:
        return True
    parts = set(p.parts)
    if parts & EXCLUDED_DIR_PARTS:
        return True
    # These five top-level directory names hold structured yaml/toml/ini config and data
    # files (docs frontmatter, install config, the registry/records/corpus stores) rather
    # than source -- excluded here, not because the suffix is unsafe to lex, but because
    # stripping "comments" out of a data file risks corrupting config a consumer parses
    # strictly, not code a proof can verify byte-equivalence against.
    if p.suffix.lower() in {".yml", ".yaml", ".toml", ".ini", ".cfg"} and p.parts and p.parts[0] in {"docs", "config", "registry", "records", "corpus"}:
        return True
    for sub in EXCLUDED_PATH_SUBSTRINGS:
        if sub in rel_path:
            return True
    return False


def is_protected(rel_path: str, repo_root: Path) -> bool:
    """True if `rel_path` is a restore-commit-protected file whose current on-disk
    bytes match one of its recorded content hashes in `protected_paths.json`.

    Membership is checked first (a cheap dict lookup); the file's bytes are only
    read and hashed on a membership hit, since only a path actually listed in
    `protected_paths.json` can ever match -- hashing every candidate unconditionally
    is wasted work for the common case of no entry."""
    protected_hashes = _load_protected_paths().get(rel_path.replace("\\", "/"))
    if not protected_hashes:
        return False
    try:
        current_bytes = (repo_root / rel_path).read_bytes()
    except OSError:
        return False
    return hashlib.sha256(current_bytes).hexdigest() in protected_hashes


def _collect_all_tokens_from_tree(repo_root: Path, files: list[str]) -> set[str]:
    """Cheap cross-file reference index: every long marker-like token appearing anywhere."""
    tokens: set[str] = set()
    marker_re = re.compile(r"[A-Z][A-Z0-9_\-]{7,}")
    for rel in files:
        fp = repo_root / rel
        try:
            text = fp.read_text(encoding="utf-8", errors="ignore")
        except (OSError, UnicodeDecodeError):
            continue
        for m in marker_re.finditer(text):
            tokens.add(m.group(0))
    return tokens


def _reads_source_as_text(text: str) -> bool:
    return bool(re.search(r"\.read_text\(|open\([^)]*['\"]r['\"]?\)", text)) and (
        "test" in text.lower()
    )


_DOC_SELF_REF_RE = re.compile(
    r"\b__doc__\b|inspect\.getdoc\(|inspect\.getsource\(|\bgetdoc\(|\bgetsource\("
)
_DOC_CROSS_REF_RE = re.compile(
    r"([A-Za-z_][\w.]*)\.__doc__\b|\bgetdoc\(\s*([A-Za-z_][\w.]*)\s*\)|\bgetsource\(\s*([A-Za-z_][\w.]*)\s*\)"
)


def _self_references_own_doc(text: str) -> bool:
    """True if a file reads `__doc__`/`inspect.getdoc`/`inspect.getsource` on itself
    (e.g. `argparse.ArgumentParser(description=__doc__)`) — every docstring in the file
    must survive, per the tooling-read keep rule."""
    return bool(_DOC_SELF_REF_RE.search(text))


def _collect_docstring_referenced_modules(repo_root: Path, files: list[str]) -> set[str]:
    """Cross-file variant: a module/object whose docstring another tracked file reads via
    `mod.__doc__`, `getdoc(mod)`, or `getsource(mod)`. Returns the set of bare names
    (last dotted component) referenced this way, matched against each candidate file's
    module stem (filename without suffix)."""
    names: set[str] = set()
    for rel in files:
        if not rel.endswith(".py"):
            continue
        fp = repo_root / rel
        try:
            text = fp.read_text(encoding="utf-8", errors="ignore")
        except (OSError, UnicodeDecodeError):
            continue
        for m in _DOC_CROSS_REF_RE.finditer(text):
            target = m.group(1) or m.group(2) or m.group(3)
            if target:
                names.add(target.split(".")[-1])
    return names


def _collapse_blank_runs(text: str) -> str:
    lines = text.split("\n")
    out: list[str] = []
    blank_run = 0
    for line in lines:
        if line.strip() == "":
            blank_run += 1
            if blank_run <= 2:
                out.append(line)
        else:
            blank_run = 0
            out.append(line)
    return "\n".join(out)


def _apply_span_removals(text: str, spans: list[tuple[int, int]]) -> str:
    """Remove [start,end) byte spans (sorted, non-overlapping) from text."""
    spans = sorted(spans)
    out = []
    last = 0
    for s, e in spans:
        out.append(text[last:s])
        last = e
    out.append(text[last:])
    return "".join(out)


@dataclass
class _MergedSpan:
    start: int
    end: int
    text: str


def _merge_adjacent_line_comment_spans(text: str, spans: list) -> list:
    """Merge contiguous standalone same-indent line-comment spans (`#`, `//`, `--`) into
    one block span before the keep/remove decision runs, so a tooling-marker or
    reference-token match on ONE line of a multi-line prose comment does not keep that
    line alone while its sibling lines -- part of the same sentence -- get removed,
    fragmenting the block into mid-sentence nonsense. A block-delimited comment
    (`/* */`, `<# #>`) already spans multiple lines by construction and is left alone;
    a trailing comment sharing a line with code is never merged into a following
    standalone comment line, since the two have unrelated keep rationale."""
    if len(spans) < 2:
        return list(spans)
    merged: list = []
    i = 0
    n = len(spans)
    while i < n:
        sp = spans[i]
        # A block-delimited comment already spans its own lines; a self-contained
        # tooling directive (`# noqa`, `@vitest-environment`, ...) is single-purpose and
        # must never be fused with an adjacent, independently-meaningful prose comment
        # just because the lines are neighbours -- only a run of plain (non-tooling)
        # lines is presumed to be one continued sentence.
        if sp.text.startswith(("/*", "<#")) or is_tooling_comment(sp.text):
            merged.append(sp)
            i += 1
            continue
        line_start = text.rfind("\n", 0, sp.start) + 1
        prefix = text[line_start:sp.start]
        if prefix.strip() != "":
            merged.append(sp)
            i += 1
            continue
        block_start = sp.start
        block_end = sp.end
        j = i + 1
        while j < n:
            nxt = spans[j]
            if nxt.text.startswith(("/*", "<#")) or is_tooling_comment(nxt.text):
                break
            nxt_line_start = text.rfind("\n", 0, nxt.start) + 1
            nxt_prefix = text[nxt_line_start:nxt.start]
            if nxt_prefix != prefix or nxt_line_start != block_end + 1:
                break
            block_end = nxt.end
            j += 1
        if j > i + 1:
            merged.append(_MergedSpan(block_start, block_end, text[block_start:block_end]))
        else:
            merged.append(sp)
        i = j
    return merged


def _merge_adjacent_comment_candidates(text: str, candidates: list) -> list:
    """Python-candidate analogue of `_merge_adjacent_line_comment_spans`, operating on
    line numbers (tokenize reports one COMMENT token per physical line) rather than byte
    offsets. Docstring candidates are untouched -- the AST already reports a docstring as
    one whole span, so they cannot fragment this way."""
    comments = [c for c in candidates if c.kind == "comment"]
    others = [c for c in candidates if c.kind != "comment"]
    if len(comments) < 2:
        return candidates
    lines = text.splitlines(keepends=True)
    comments.sort(key=lambda c: c.line_start)
    merged = []
    i = 0
    n = len(comments)
    while i < n:
        c = comments[i]
        line = lines[c.line_start - 1]
        hash_idx = line.find("#")
        prefix = line[:hash_idx] if hash_idx != -1 else line
        if prefix.strip() != "" or is_tooling_comment(c.text):
            merged.append(c)
            i += 1
            continue
        end_line = c.line_end
        texts = [c.text]
        j = i + 1
        while j < n:
            nxt = comments[j]
            if nxt.line_start != end_line + 1 or is_tooling_comment(nxt.text):
                break
            nxt_line = lines[nxt.line_start - 1]
            nxt_hash_idx = nxt_line.find("#")
            nxt_prefix = nxt_line[:nxt_hash_idx] if nxt_hash_idx != -1 else nxt_line
            if nxt_prefix != prefix:
                break
            texts.append(nxt.text)
            end_line = nxt.line_end
            j += 1
        merged.append(lang_python.Candidate(c.line_start, end_line, "".join(texts), "comment"))
        i = j
    return others + merged


def _plan_generic(text: str, spans_raw: list, whole_file_reference_keep: bool) -> FileResult | None:
    keep_spans: list[tuple[int, int]] = []
    remove_spans: list[tuple[int, int]] = []
    kept_tooling = 0
    kept_reference = 0
    for sp in spans_raw:
        body = sp.text
        if is_tooling_comment(body):
            kept_tooling += 1
            keep_spans.append((sp.start, sp.end))
            continue
        if whole_file_reference_keep:
            kept_reference += 1
            keep_spans.append((sp.start, sp.end))
            continue
        remove_spans.append((sp.start, sp.end))
    return remove_spans, kept_tooling, kept_reference


def plan_file(
    repo_root: Path, rel_path: str, referenced_tokens: set[str],
    docstring_referenced_modules: frozenset[str] = frozenset(),
) -> FileResult:
    fp = repo_root / rel_path
    suffix = fp.suffix.lower()
    lang = LANG_BY_SUFFIX.get(suffix)
    try:
        text = fp.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        return FileResult(rel_path, lang or "unknown", False, skipped_reason=f"read-error: {exc}")

    whole_file_ref_keep = _reads_source_as_text(text)

    if lang is None:
        return FileResult(rel_path, "unknown", False, skipped_reason="unsupported-suffix")

    if lang == "python":
        doc_keep = whole_file_ref_keep or _self_references_own_doc(text) or (
            Path(rel_path).stem in docstring_referenced_modules
        )
        return _plan_python(rel_path, text, referenced_tokens, whole_file_ref_keep, doc_keep)

    if lang in ("ts", "tsx", "js"):
        return _plan_ts(repo_root, rel_path, text, lang, referenced_tokens, whole_file_ref_keep)

    if lang == "clike":
        spans = lang_clike.find_comment_spans(text, cpp_raw_strings=True)
        return _finish_generic_lex(rel_path, "clike", text, spans, referenced_tokens, whole_file_ref_keep)

    if lang == "clike-noline":
        spans = lang_clike.find_comment_spans(text, line_comment="")
        return _finish_generic_lex(rel_path, "css", text, spans, referenced_tokens, whole_file_ref_keep)

    if lang == "rust":
        spans = lang_clike.find_comment_spans(text, rust_raw_strings=True)
        return _finish_generic_lex(rel_path, "rust", text, spans, referenced_tokens, whole_file_ref_keep)

    if lang == "sql":
        spans = lang_clike.find_comment_spans(text, line_comment="", sql_line_comment=False)
        spans += lang_clike.find_comment_spans(text, line_comment="--", block_comment=False)
        return _finish_generic_lex(rel_path, "sql", text, sorted(spans, key=lambda s: s.start), referenced_tokens, whole_file_ref_keep)

    if lang == "shell":
        spans = lang_hash.find_comment_spans(text, shell_heredocs=True)
        return _finish_generic_lex(rel_path, "shell", text, spans, referenced_tokens, whole_file_ref_keep)

    if lang == "powershell":
        spans = lang_hash.find_comment_spans(text, powershell_block=True)
        return _finish_generic_lex(rel_path, "powershell", text, spans, referenced_tokens, whole_file_ref_keep)

    if lang == "hash":
        spans = lang_hash.find_comment_spans(text)
        return _finish_generic_lex(rel_path, "yaml/toml/ini", text, spans, referenced_tokens, whole_file_ref_keep)

    SKIPPED_LANGS_REPORTED.add(lang)
    return FileResult(rel_path, lang, False, skipped_reason="no-safe-lexer")


def _finish_generic_lex(rel_path, lang, text, spans, referenced_tokens, whole_file_ref_keep, proof_fn=None) -> FileResult:
    spans = _merge_adjacent_line_comment_spans(text, spans)
    remove_spans = []
    kept_tooling = 0
    kept_reference = 0
    for sp in spans:
        body = sp.text
        if is_tooling_comment(body):
            kept_tooling += 1
            continue
        markers = extract_marker_tokens(body)
        if whole_file_ref_keep or any(m in referenced_tokens for m in markers):
            kept_reference += 1
            continue
        remove_spans.append((sp.start, sp.end))

    if not remove_spans:
        return FileResult(rel_path, lang, False, 0, kept_tooling, kept_reference, True, new_text=text)

    new_text = _apply_span_removals(text, remove_spans)
    proof_ok = proof_fn(text, new_text) if proof_fn is not None else _proof_generic(text, new_text, lang)
    if proof_ok:
        new_text = _collapse_blank_runs(new_text)
    return FileResult(
        rel_path, lang, proof_ok, len(remove_spans), kept_tooling, kept_reference,
        proof_ok, new_text=new_text if proof_ok else None,
        skipped_reason=None if proof_ok else "proof-failed",
    )


def _strip_all_comments_from_text(text: str, lang: str) -> str:
    """Re-lex `text` and strip ALL comment spans (used only for the proof, not the plan)."""
    if lang == "clike":
        spans = lang_clike.find_comment_spans(text)
    elif lang == "css":
        spans = lang_clike.find_comment_spans(text, line_comment="")
    elif lang == "rust":
        spans = lang_clike.find_comment_spans(text, rust_raw_strings=True)
    elif lang == "sql":
        spans = lang_clike.find_comment_spans(text, line_comment="")
        spans += lang_clike.find_comment_spans(text, line_comment="--", block_comment=False)
        spans = sorted(spans, key=lambda s: s.start)
    elif lang == "shell":
        spans = lang_hash.find_comment_spans(text, shell_heredocs=True)
    elif lang == "powershell":
        spans = lang_hash.find_comment_spans(text, powershell_block=True)
    else:
        spans = lang_hash.find_comment_spans(text)
    return _apply_span_removals(text, [(s.start, s.end) for s in spans])


def _proof_generic(original: str, new_text: str, lang: str) -> bool:
    """Proof: the non-comment token stream of `new_text` (which has no comments left by
    construction if it came from removing a subset) must equal stripping ALL comments from
    the original. Since `new_text` retains some comments (the kept ones), re-strip both
    fully and compare — this proves no non-comment byte moved."""
    stripped_new = _strip_all_comments_from_text(new_text, lang)
    stripped_orig = _strip_all_comments_from_text(original, lang)
    return stripped_new == stripped_orig


def _plan_python(
    rel_path: str, text: str, referenced_tokens: set[str], whole_file_ref_keep: bool,
    doc_keep: bool | None = None,
) -> FileResult:
    if doc_keep is None:
        doc_keep = whole_file_ref_keep
    candidates = lang_python.collect_candidates(text)
    if not candidates:
        return FileResult(rel_path, "python", False, new_text=text)
    candidates = _merge_adjacent_comment_candidates(text, candidates)

    lines = text.splitlines(keepends=True)

    remove_line_spans: list[tuple[int, int]] = []  # (line_start_idx0, line_end_idx0_exclusive)
    kept_tooling = 0
    kept_reference = 0
    docstring_lines_to_remove: set[int] = set()
    docstring_start_lines: set[int] = set()
    docstring_pass_indent: dict[int, str] = {}  # line_start -> indent, needs `pass` inserted

    for cand in candidates:
        body = cand.text
        if is_tooling_comment(body):
            kept_tooling += 1
            continue
        markers = extract_marker_tokens(body)
        if whole_file_ref_keep or any(m in referenced_tokens for m in markers):
            kept_reference += 1
            continue
        if cand.kind == "docstring":
            if doc_keep:
                kept_reference += 1
                continue
            docstring_start_lines.add(cand.line_start)
            for ln in range(cand.line_start, cand.line_end + 1):
                docstring_lines_to_remove.add(ln)
            if cand.sole_body:
                docstring_pass_indent[cand.line_start] = cand.indent
        else:
            remove_line_spans.append((cand.line_start, cand.line_end))

    if not remove_line_spans and not docstring_lines_to_remove:
        return FileResult(rel_path, "python", False, 0, kept_tooling, kept_reference, True, new_text=text)

    remove_lines: set[int] = set(docstring_lines_to_remove)
    comment_removed_count = 0
    for s, e in remove_line_spans:
        for ln in range(s, e + 1):
            remove_lines.add(ln)
        comment_removed_count += 1

    comment_only_line_set = {l for s, e in remove_line_spans for l in range(s, e + 1)}
    new_lines = []
    for idx, line in enumerate(lines, start=1):
        if idx in remove_lines:
            if idx in docstring_lines_to_remove and idx not in comment_only_line_set:
                if idx in docstring_pass_indent:
                    new_lines.append(f"{docstring_pass_indent[idx]}pass\n")
                continue
            # For a pure comment-only line, drop entirely; for a line with a trailing comment,
            # strip only the comment portion.
            comment_only = line.strip().startswith("#")
            if comment_only or idx in docstring_lines_to_remove:
                continue
            new_lines.append(_strip_trailing_comment(line))
        else:
            new_lines.append(line)

    new_text = "".join(new_lines)
    new_text = _collapse_blank_runs(new_text)

    proof_ok = lang_python.proof_equivalent(text, new_text, docstring_start_lines)
    if not proof_ok:
        return FileResult(rel_path, "python", False, 0, kept_tooling, kept_reference, False,
                           skipped_reason="proof-failed")

    removed_count = comment_removed_count + len(docstring_start_lines)
    return FileResult(rel_path, "python", True, removed_count, kept_tooling, kept_reference, True, new_text=new_text)


def _strip_trailing_comment(line: str) -> str:
    """Remove a trailing `# ...` comment from a code line without touching string contents.
    Uses `tokenize` line-local re-scan is overkill; caller only invokes this for lines that
    `tokenize` already identified as containing a COMMENT token, so a rightmost unquoted
    `#` search is safe given the file already parsed cleanly."""
    in_str = None
    i = 0
    while i < len(line):
        c = line[i]
        if in_str:
            if c == "\\":
                i += 2
                continue
            if c == in_str:
                in_str = None
            i += 1
            continue
        if c in ("'", '"'):
            in_str = c
            i += 1
            continue
        if c == "#":
            rest = line[:i].rstrip()
            return (rest + "\n") if line.endswith("\n") else rest
        i += 1
    return line


class _TsWarmProc:
    """One long-lived `node scan_ts.js --batch` process per `ts_pkg`, fed NDJSON requests
    over stdin/stdout. Replaces a fresh node spawn (and fresh `require(typescript)`, the
    dominant per-file cost) per scanned file — see dispatch brief item 5."""

    _procs: dict[str, "_TsWarmProc"] = {}

    def __init__(self, ts_pkg: str):
        self._proc = subprocess.Popen(
            ["node", str(_SCAN_TS_JS), ts_pkg, "--batch"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, bufsize=1,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        self._next_id = 0

    def _call(self, payload: dict) -> dict | None:
        if self._proc.stdin is None or self._proc.stdout is None or self._proc.poll() is not None:
            return None
        req_id = self._next_id
        self._next_id += 1
        payload = {**payload, "id": req_id}
        try:
            self._proc.stdin.write(json.dumps(payload) + "\n")
            self._proc.stdin.flush()
            line = self._proc.stdout.readline()
            if not line:
                return None
            resp = json.loads(line)
        except (OSError, ValueError):
            return None
        if resp.get("error") is not None or resp.get("id") != req_id:
            return None
        return resp

    def scan(self, variant: str, text: str) -> list[dict] | None:
        resp = self._call({"mode": "scan", "variant": variant, "text": text})
        return None if resp is None else resp.get("spans")

    def verify(self, variant: str, orig_text: str, new_text: str) -> bool | None:
        resp = self._call({"mode": "verify", "variant": variant, "text": orig_text, "newText": new_text})
        return None if resp is None else resp.get("equal")

    def close(self) -> None:
        try:
            if self._proc.stdin:
                self._proc.stdin.close()
            self._proc.terminate()
        except OSError:
            pass

    @classmethod
    def get(cls, ts_pkg: str) -> "_TsWarmProc":
        proc = cls._procs.get(ts_pkg)
        if proc is None or proc._proc.poll() is not None:
            proc = cls(ts_pkg)
            cls._procs[ts_pkg] = proc
        return proc

    @classmethod
    def close_all(cls) -> None:
        for proc in cls._procs.values():
            proc.close()
        cls._procs.clear()


def _plan_ts(repo_root: Path, rel_path: str, text: str, lang: str, referenced_tokens, whole_file_ref_keep) -> FileResult:
    ts_pkg = _find_ts_pkg()
    if ts_pkg is None:
        SKIPPED_LANGS_REPORTED.add(lang)
        return FileResult(rel_path, lang, False, skipped_reason="no-typescript-package-available")
    variant = "jsx" if rel_path.endswith((".tsx", ".jsx")) else "standard"
    raw = _TsWarmProc.get(ts_pkg).scan(variant, text)
    if raw is None:
        return FileResult(rel_path, lang, False, skipped_reason="ts-scan-failed")

    class _Sp:
        __slots__ = ("start", "end", "text")

        def __init__(self, d):
            self.start, self.end, self.text = d["start"], d["end"], d["text"]

    spans = [_Sp(d) for d in raw]
    return _finish_generic_lex(
        rel_path, lang, text, spans, referenced_tokens, whole_file_ref_keep,
        proof_fn=lambda orig, new: _proof_ts(ts_pkg, variant, orig, new),
    )


def _proof_ts(ts_pkg: str, variant: str, original: str, new_text: str) -> bool:
    """Proof: parse both `original` and `new_text` with the real TypeScript parser and
    require an identical `ts.createPrinter({removeComments:true})` print, with an equal
    parse-diagnostic count (a file that only parses "successfully" by accident — e.g. a
    comment whose prose contains an early-terminating `*/` — must not silently pass). This
    is the AST-level proof, not a re-lex-and-diff: a raw re-scan cannot tell a `//`/`*/`
    inside a template-literal substitution from a real comment (see scan_ts.js's header),
    so it must never be trusted as the proof, only as the span source for planning."""
    equal = _TsWarmProc.get(ts_pkg).verify(variant, original, new_text)
    return bool(equal)


_TS_PKG_CACHE: str | None = "__unset__"


def _find_ts_pkg() -> str | None:
    """Locate an already-vendored `typescript` package to drive the real TS scanner.
    Resolved relative to `repos.project_rag` (never a hardcoded absolute path) — this repo
    vendors `scip-typescript`, which carries its own `typescript` under node_modules."""
    global _TS_PKG_CACHE
    if _TS_PKG_CACHE != "__unset__":
        return _TS_PKG_CACHE
    import os
    import shutil

    settings_home = os.environ.get("COORDINATOR_SETTINGS_HOME") or os.environ.get("CLAUDE_HOME") or str(Path.home())
    settings_home = settings_home if settings_home.endswith(".coordinator-claude-settings") else str(Path(settings_home) / ".coordinator-claude-settings")
    ml_bin = shutil.which("machine-local") or str(Path(settings_home) / "bin" / "machine-local")
    example_retrieval_repo = None
    try:
        out = subprocess.run(
            [ml_bin, "get", "repos.project_rag"],
            capture_output=True, text=True, timeout=10, check=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        example_retrieval_repo = out.stdout.strip() or None
    except (subprocess.SubprocessError, OSError):
        example_retrieval_repo = None
    candidates = []
    if example_retrieval_repo is not None:
        candidates.append(Path(example_retrieval_repo) / "vendor/scip-typescript/node_modules/typescript")
    for c in candidates:
        if c.exists():
            _TS_PKG_CACHE = str(c)
            return _TS_PKG_CACHE
    _TS_PKG_CACHE = None
    return None


def _git_status_porcelain(repo_root: Path) -> str:
    out = subprocess.run(
        ["git", "-C", str(repo_root), "status", "--porcelain"],
        capture_output=True, text=True, check=True,
        **no_console_creationflags(),
    )
    return out.stdout


def _refused_apply_summary(repo_root: Path, gate: str) -> dict:
    """Minimal refusal summary -- only the keys a caller actually reads before
    touching any bytes (`bin/strip-comments.py`'s CLI output line, plus
    `gate`/`files_changed` for the dirty-tree test). "Not a hand-copy" refers to the keys
    this dict OMITS relative to the full scan summary -- no file was scanned, so there is
    nothing to report on `files`, `skipped_*`, `new_failures`, or `peer_skipped` -- not to
    the six keys it shares (`repo`/`apply`/`files_scanned`/`files_changed`/
    `comment_lines_removed`/`kept_reference`), which ARE duplicated literally on purpose."""
    return {
        "repo": str(repo_root),
        "apply": True,
        "gate": gate,
        "files_scanned": 0,
        "files_changed": 0,
        "comment_lines_removed": 0,
        "kept_reference": 0,
        "proof_failures": [],
    }


def strip_repo(
    repo_root: Path, report_path: Path | None = None, apply: bool = False,
) -> dict:
    """Scan and (with `apply=True`) rewrite a repo's tracked source files.

    `apply=True` refuses outright -- before touching any bytes -- if `git status
    --porcelain` for the whole tree is non-empty (any uncommitted tracked change,
    anywhere, not just files this strip would write). Every written file's original
    bytes are recorded via `read_bytes()` before the write; restoring later writes
    those bytes back with `write_bytes` (never `read_text`/`write_text` anywhere in
    this path), so CRLF/BOM originals round-trip byte-for-byte on refuse/fail.

    On a real apply (bytes actually changed), the gate (`run_gate`) runs the
    candidate command against the already-stripped tree, then this module's own
    `restore_originals` callback puts the recorded original bytes back (skipping,
    and reporting under `peer_skipped`, any file whose on-disk bytes no longer match
    what the stripper wrote -- a concurrent peer edit is never clobbered), the gate
    runs the base command, and on a clean diff `reapply_stripped` writes the stripped
    bytes back.
    """
    from coordinator_core.attribution import is_exempt_path

    if apply:
        dirty = _git_status_porcelain(repo_root)
        if dirty.strip():
            return _refused_apply_summary(repo_root, "refused-dirty-tree")

    out = subprocess.run(
        ["git", "-C", str(repo_root), "ls-files"], capture_output=True, text=True, check=True,
        **no_console_creationflags(),
    )
    all_files = [f for f in out.stdout.splitlines() if f]
    candidates = [f for f in all_files if not is_exempt_path(f) and not is_excluded_path(f)]
    candidates = [f for f in candidates if Path(f).suffix.lower() in LANG_BY_SUFFIX]

    referenced_tokens = _collect_all_tokens_from_tree(repo_root, all_files)
    docstring_referenced_modules = frozenset(_collect_docstring_referenced_modules(repo_root, all_files))

    results: list[FileResult] = []
    original_bytes: dict[str, bytes] = {}
    written_bytes: dict[str, bytes] = {}
    peer_skipped: list[str] = []
    restored_files: set[str] = set()
    restored_flag = {"done": False}

    def _restore_originals() -> None:
        """Idempotent and resumable: `restored_files` tracks exactly which files this
        call (or a prior call, on retry) has already written back, so a `write_bytes`
        raising mid-loop -- leaving `restored_flag["done"]` False -- can be safely
        retried from where it left off. Retrying naively from the top without this
        tracking would re-run the peer-edit check against already-restored files, whose
        current bytes are now the *original* bytes rather than `written_bytes[rel]`,
        and misclassify every already-restored file as a mid-run peer edit. Setting
        `restored_flag["done"]` is the LAST step, only once every file has been either
        restored or (correctly) recognized as peer-edited -- never a pre-emptive marker,
        since the outer `except BaseException` retry in `strip_repo` keys off it to
        decide whether a further restore attempt is owed."""
        for rel, orig in original_bytes.items():
            if rel in peer_skipped or rel in restored_files:
                continue
            fp = repo_root / rel
            try:
                current = fp.read_bytes()
            except OSError:
                current = None
            if current != written_bytes.get(rel):
                # A peer edited this file mid-run -- leave it alone, never clobber it.
                peer_skipped.append(rel)
                continue
            fp.write_bytes(orig)
            restored_files.add(rel)
        if len(restored_files) + len(peer_skipped) == len(original_bytes):
            restored_flag["done"] = True

    def _reapply_stripped() -> None:
        """Writes the stripped bytes back, and un-marks every file it just
        rewrote from `restored_files` -- `run_gate`'s own confirmation path
        (`_confirm_new_failures`) legitimately calls `restore_originals()`,
        then `reapply_stripped()`, then `restore_originals()` again within a
        single gate run. Without clearing `restored_files` here, that second
        `restore_originals()` call would see every file already marked
        restored (from the first call) and skip it as a no-op, leaving the
        tree at candidate (stripped) bytes for what is supposed to be the
        base-bytes confirmation run -- silently turning a real regression
        into a false 'pre-existing, dropped' verdict."""
        for rel, data in written_bytes.items():
            if rel in peer_skipped:
                continue
            (repo_root / rel).write_bytes(data)
            restored_files.discard(rel)

    gate_info = {"gate": "skipped", "new_failures": []}
    try:
        for rel in candidates:
            if is_protected(rel, repo_root):
                # Protected: a restored file the strip previously broke a test on.
                # Kept outright, never scanned-and-changed.
                results.append(FileResult(path=rel, language="protected", changed=False, skipped_reason="protected"))
                continue
            res = plan_file(repo_root, rel, referenced_tokens, docstring_referenced_modules)
            results.append(res)
            if apply and res.changed and res.new_text is not None:
                fp = repo_root / rel
                original_bytes[rel] = fp.read_bytes()
                new_bytes = res.new_text.encode("utf-8")
                fp.write_bytes(new_bytes)
                written_bytes[rel] = new_bytes

        if apply and written_bytes:
            file_bytes = {rel: (original_bytes[rel], written_bytes[rel]) for rel in written_bytes}
            gate_result = run_gate(
                str(repo_root),
                list(written_bytes),
                restore_originals=_restore_originals,
                reapply_stripped=_reapply_stripped,
                file_bytes=file_bytes,
            )
            gate_info = {"gate": gate_result.verdict, "new_failures": list(gate_result.new_failures)}
    except BaseException:
        # Exception mid-loop, or Ctrl-C during the gate's own multi-minute run, before
        # its documented restore_originals() call landed -- still triggers a restore.
        if apply and original_bytes and not restored_flag["done"]:
            _restore_originals()
        raise
    finally:
        _TsWarmProc.close_all()

    summary = {
        "repo": str(repo_root),
        "apply": apply,
        "files_scanned": len(results),
        "files_changed": sum(1 for r in results if r.changed),
        "comment_lines_removed": sum(r.removed_count for r in results if r.changed),
        "kept_tooling": sum(r.kept_tooling_count for r in results),
        "kept_reference": sum(r.kept_reference_count for r in results),
        "proof_failures": [r.path for r in results if r.skipped_reason == "proof-failed"],
        "skipped_unsupported": sorted({r.path for r in results if r.skipped_reason == "no-safe-lexer"}),
        "skipped_languages": sorted(SKIPPED_LANGS_REPORTED),
        "files": [
            {
                "path": r.path,
                "language": r.language,
                "changed": r.changed,
                "removed": r.removed_count,
                "kept_tooling": r.kept_tooling_count,
                "kept_reference": r.kept_reference_count,
                "skipped_reason": r.skipped_reason,
            }
            for r in results
        ],
    }
    if apply:
        summary.update(gate_info)
        summary["peer_skipped"] = list(peer_skipped)
    if report_path is not None:
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    return summary
