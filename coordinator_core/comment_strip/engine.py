"""Orchestrates per-file comment-strip planning with a mechanical no-code-change proof.

Each language module reports raw comment spans; this module applies the KEEP rules, removes
everything else, collapses resulting blank-line runs, and re-verifies the result via a
language-appropriate proof before returning a plan. Nothing here writes to disk — the CLI
applies plans when `--apply` is passed.
"""
from __future__ import annotations

import ast
import json
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

from . import lang_clike, lang_hash, lang_python
from .keep_rules import extract_marker_tokens, is_tooling_comment

_SCAN_TS_JS = Path(__file__).with_name("scan_ts.js")

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


def is_excluded_path(rel_path: str) -> bool:
    p = Path(rel_path)
    if p.suffix.lower() in EXCLUDED_SUFFIXES:
        return True
    parts = set(p.parts)
    if parts & EXCLUDED_DIR_PARTS:
        return True
    if p.suffix.lower() in {".yml", ".yaml", ".toml", ".ini", ".cfg"} and p.parts and p.parts[0] in {"docs", "config", "registry", "records", "corpus"}:
        return True
    for sub in EXCLUDED_PATH_SUBSTRINGS:
        if sub in rel_path:
            return True
    return False


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


def strip_repo(repo_root: Path, report_path: Path | None = None, apply: bool = False) -> dict:
    from coordinator_core.attribution import is_exempt_path

    out = subprocess.run(["git", "-C", str(repo_root), "ls-files"], capture_output=True, text=True, check=True)
    all_files = [f for f in out.stdout.splitlines() if f]
    candidates = [f for f in all_files if not is_exempt_path(f) and not is_excluded_path(f)]
    candidates = [f for f in candidates if Path(f).suffix.lower() in LANG_BY_SUFFIX]

    referenced_tokens = _collect_all_tokens_from_tree(repo_root, all_files)
    docstring_referenced_modules = frozenset(_collect_docstring_referenced_modules(repo_root, all_files))

    results: list[FileResult] = []
    try:
        for rel in candidates:
            res = plan_file(repo_root, rel, referenced_tokens, docstring_referenced_modules)
            results.append(res)
            if apply and res.changed and res.new_text is not None:
                (repo_root / rel).write_text(res.new_text, encoding="utf-8")
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
    if report_path is not None:
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    return summary
