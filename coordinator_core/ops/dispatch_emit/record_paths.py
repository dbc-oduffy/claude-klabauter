"""record_paths: the one set of non-product record prefixes shared by Python and the emitted JS.

`is_record_path` and `record_prefix_js_regex` read `RECORD_PREFIXES`, so the review-skip gate that
`review_mint.execute_review` emits and the Python-side review prep cannot disagree on what a record is.
Pure string work: no git, no filesystem, no subprocess.
"""

from __future__ import annotations

#: Top-level directories whose files are records, never product. Order is the emitted regex order.
RECORD_PREFIXES: tuple[str, ...] = (
    "state",
    "archive",
    "scratch",
    "tasks",
    "cross-repo",
    "docs",
    ".coordinator-local",
)


def _normalise(rel: str) -> str:
    """Posix form: backslashes to `/`, leading `./` stripped. Never touches the filesystem."""
    p = str(rel).replace("\\", "/")
    while p.startswith("./"):
        p = p[2:]
    return p


def is_record_path(rel: str) -> bool:
    """True when `rel` is a record: under a `RECORD_PREFIXES` directory, or any `.md` file, and
    `review_floor` owes no review on it (`review_owed_paths`), so a skipped path set is never refused
    by the floor for zero verdicts.

    A bare prefix name with no trailing path (`state`) is not under the directory, matching the JS
    `^(prefix)\\/` test.
    """
    from coordinator_core.ops.dispatch_emit.review_floor import review_owed_paths

    p = _normalise(rel)
    if not (p.endswith(".md") or any(p.startswith(prefix + "/") for prefix in RECORD_PREFIXES)):
        return False
    return not review_owed_paths([p])


def record_prefix_js_regex() -> str:
    """The JS regex source (no delimiters) matching a path under a record prefix: `^(a|b)\\/`.

    Embed it between `/` delimiters in emitted JS; only `.` needs escaping in the current prefix set
    (`-` is left bare so the source stays valid under the JS `u` flag).
    """
    alternation = "|".join(p.replace(".", "\\.") for p in RECORD_PREFIXES)
    return "^(" + alternation + ")\\/"
