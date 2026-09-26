"""
The commit-surface roster for docs/plans/2026-08-27-the-commit-op-stops-narrating-how-it-got-here.md.

Single source of truth for "which files does that plan's commit surface cover", "which lines on
them count as narration", and "which named symbols must never be deleted" -- consumed by the
fast-tier ratchet (test_commit_surface_budget.py) and the dated audit script
(state/audits/2026-08-27-git-ceremony-narrative-and-linecount-falsifier.py) so neither derives its
own roster or its own marker policy.

COMMIT_SURFACE_FILES is a CURATED CONSTANT, not a predicate over disk: no mechanical rule over
coordinator_core/git/ and coordinator_core/ops/ceremony/ yields these seven and only these seven
(22 non-test modules sit under the first directory, 28 under the second; the sized rows below name
2 and 5). EXCLUDED_MODULES enumerates every other non-test module in those two directories so the
pairing is checkable by construction: a module in neither constant is a narration holder nobody
classified.
"""
import io
import re
import tokenize

COMMIT_SURFACE_FILES = {
    "coordinator_core/ops/ceremony/git_native.py":
        "largest ceremony narration holder (5,726 lines); grew from 5,376 while commit_pipeline.py "
        "was deleted out from under this plan; absorbed narration from the vanished module.",
    "coordinator_core/ops/ceremony/commit_gates.py":
        "gate predicates guarding the commit route; named in the pre-revision scope and still live "
        "at HEAD (1,107 lines).",
    "coordinator_core/ops/ceremony/push.py":
        "one of the three modules that now hold commit_pipeline.py's narration after its deletion "
        "(1,894 lines).",
    "coordinator_core/ops/ceremony/commit_v2.py":
        "the live commit route's ceremony entry point (ceremony.commit_v2 -> "
        "coordinator_core/git/commit.py :: commit_paths); one of the three modules that absorbed "
        "commit_pipeline.py's narration (754 lines).",
    "coordinator_core/ops/ceremony/tail_ops.py":
        "one of the three modules that now hold commit_pipeline.py's narration after its deletion "
        "(709 lines).",
    "coordinator_core/git/git_state.py":
        "named in the pre-revision scope and still live at HEAD (924 lines).",
    "coordinator_core/git/git_index.py":
        "named in the pre-revision scope and still live at HEAD (473 lines).",
}

# HEAD (2026-09-10 census), each with a one-line reason it is not in COMMIT_SURFACE_FILES. This is
EXCLUDED_MODULES = {
    "coordinator_core/git/__init__.py":
        "package marker; not a narration holder.",
    "coordinator_core/git/action_guard.py":
        "non-roster module under coordinator_core/git/; unsized by this plan's scope:.",
    "coordinator_core/git/argv_batch.py":
        "non-roster module under coordinator_core/git/; unsized by this plan's scope:.",
    "coordinator_core/git/checkin_attrs.py":
        "non-roster module under coordinator_core/git/; unsized by this plan's scope:.",
    "coordinator_core/git/commit.py":
        "the live commit route (coordinator_core/git/commit.py :: commit_paths), 10 marker lines, "
        "the peer's active rewrite surface under external_gate[0]; no deletion row here.",
    "coordinator_core/git/commit_context.py":
        "non-roster module under coordinator_core/git/; unsized by this plan's scope:.",
    "coordinator_core/git/commit_delta.py":
        "non-roster module under coordinator_core/git/; unsized by this plan's scope:.",
    "coordinator_core/git/commit_trailers.py":
        "30 marker lines -- more than any roster file but git_native.py -- and likewise unsized; "
        "not in scope: for this plan.",
    "coordinator_core/git/commit_walk.py":
        "non-roster module under coordinator_core/git/; unsized by this plan's scope:.",
    "coordinator_core/git/content_hash.py":
        "non-roster module under coordinator_core/git/; unsized by this plan's scope:.",
    "coordinator_core/git/divergence.py":
        "non-roster module under coordinator_core/git/; unsized by this plan's scope:.",
    "coordinator_core/git/eol_declared.py":
        "non-roster module under coordinator_core/git/; unsized by this plan's scope:.",
    "coordinator_core/git/git_dir.py":
        "non-roster module under coordinator_core/git/; unsized by this plan's scope:.",
    "coordinator_core/git/git_objects.py":
        "non-roster module under coordinator_core/git/; unsized by this plan's scope:.",
    "coordinator_core/git/index_write.py":
        "non-roster module under coordinator_core/git/; unsized by this plan's scope:.",
    "coordinator_core/git/ls_files.py":
        "non-roster module under coordinator_core/git/; unsized by this plan's scope:.",
    "coordinator_core/git/ls_files_bytes.py":
        "non-roster module under coordinator_core/git/; unsized by this plan's scope:.",
    "coordinator_core/git/remote_url.py":
        "non-roster module under coordinator_core/git/; unsized by this plan's scope:.",
    "coordinator_core/git/repo_root.py":
        "non-roster module under coordinator_core/git/; unsized by this plan's scope:.",
    "coordinator_core/git/run.py":
        "18 marker lines; non-roster module under coordinator_core/git/; unsized by this plan's "
        "scope:.",
    "coordinator_core/git/tree_spine.py":
        "non-roster module under coordinator_core/git/; unsized by this plan's scope:.",
    "coordinator_core/ops/ceremony/__init__.py":
        "package marker; not a narration holder.",
    "coordinator_core/ops/ceremony/branch_resolution.py":
        "30 marker lines; non-roster module under coordinator_core/ops/ceremony/; unsized by this "
        "plan's scope:.",
    "coordinator_core/ops/ceremony/chunk_commits.py":
        "non-roster module under coordinator_core/ops/ceremony/; unsized by this plan's scope:.",
    "coordinator_core/ops/ceremony/commit_exec_bit.py":
        "non-roster module under coordinator_core/ops/ceremony/; unsized by this plan's scope:.",
    "coordinator_core/ops/ceremony/commit_message.py":
        "non-roster module under coordinator_core/ops/ceremony/; unsized by this plan's scope:.",
    "coordinator_core/ops/ceremony/commit_reconcile.py":
        "non-roster module under coordinator_core/ops/ceremony/; unsized by this plan's scope:.",
    "coordinator_core/ops/ceremony/completion_entry.py":
        "non-roster module under coordinator_core/ops/ceremony/; unsized by this plan's scope:.",
    "coordinator_core/ops/ceremony/consumed_handoff_stamp.py":
        "60 marker lines -- the heaviest single non-roster module; unsized by this plan's scope:.",
    "coordinator_core/ops/ceremony/detached_render_commit.py":
        "non-roster module under coordinator_core/ops/ceremony/; unsized by this plan's scope:.",
    "coordinator_core/ops/ceremony/detached_spawn.py":
        "non-roster module under coordinator_core/ops/ceremony/; unsized by this plan's scope:.",
    "coordinator_core/ops/ceremony/housekeeping_liveness.py":
        "non-roster module under coordinator_core/ops/ceremony/; unsized by this plan's scope:.",
    "coordinator_core/ops/ceremony/node_handlers.py":
        "non-roster module under coordinator_core/ops/ceremony/; unsized by this plan's scope:.",
    "coordinator_core/ops/ceremony/pipeline_context.py":
        "non-roster module under coordinator_core/ops/ceremony/; unsized by this plan's scope:.",
    "coordinator_core/ops/ceremony/post_commit_tail.py":
        "39 marker lines; non-roster module under coordinator_core/ops/ceremony/; unsized by this "
        "plan's scope:.",
    "coordinator_core/ops/ceremony/receipt_emit.py":
        "non-roster module under coordinator_core/ops/ceremony/; unsized by this plan's scope:.",
    "coordinator_core/ops/ceremony/receipt_render.py":
        "non-roster module under coordinator_core/ops/ceremony/; unsized by this plan's scope:.",
    "coordinator_core/ops/ceremony/receipt_schema.py":
        "non-roster module under coordinator_core/ops/ceremony/; unsized by this plan's scope:.",
    "coordinator_core/ops/ceremony/records_query.py":
        "non-roster module under coordinator_core/ops/ceremony/; unsized by this plan's scope:.",
    "coordinator_core/ops/ceremony/renderers.py":
        "20 marker lines; non-roster module under coordinator_core/ops/ceremony/; unsized by this "
        "plan's scope:.",
    "coordinator_core/ops/ceremony/resolver.py":
        "non-roster module under coordinator_core/ops/ceremony/; unsized by this plan's scope:.",
    "coordinator_core/ops/ceremony/snapshot_diff_and_head.py":
        "non-roster module under coordinator_core/ops/ceremony/; unsized by this plan's scope:.",
    "coordinator_core/ops/ceremony/update_docs_scan.py":
        "non-roster module under coordinator_core/ops/ceremony/; unsized by this plan's scope:.",
    "coordinator_core/ops/ceremony/wsc_disposition.py":
        "non-roster module under coordinator_core/ops/ceremony/; unsized by this plan's scope:.",
}


MARKER_REGEX = re.compile(
    r"(\b20[0-9]{2}-[0-9]{2}-[0-9]{2}\b|\bDR-[0-9]{2,5}\b|\bC[0-9]{2,4}\b|docs/(plans|briefs|decisions)/)"
)

# Case-INSENSITIVE, everywhere. C2's widened list.
KEYWORDS = [
    "MUST NOT", "NEVER", "DO NOT", "MANDATORY", "does not work", "does not",
    "must never", "stays dead", "barred", "forbidden",
]
KEYWORD_REGEX = re.compile("|".join(re.escape(k) for k in KEYWORDS), re.IGNORECASE)

WINDOW = 6

DELETED_SYMBOLS = (
    "ceremony.commit",
    "ceremony.scoped_git_commit",
    "commit_pipeline.py",
    "run_commit_pipeline",
)


def deleted_symbols():
    return list(DELETED_SYMBOLS)


def classify(path):
    with open(path, "r", encoding="utf-8") as f:
        src = f.read()
    lines = src.splitlines()
    total = len(lines)

    code_lines = set()
    comment_lines = set()
    string_lines = set()

    toks = list(tokenize.generate_tokens(io.StringIO(src).readline))

    prev_significant = None
    for tok in toks:
        ttype, tstr, start, end, line = tok
        if ttype == tokenize.COMMENT:
            comment_lines.add(start[0])
        elif ttype == tokenize.STRING:
            is_standalone = prev_significant is None or prev_significant[0] in (
                tokenize.NEWLINE, tokenize.INDENT, tokenize.DEDENT, tokenize.NL, tokenize.ENCODING,
            )
            for ln in range(start[0], end[0] + 1):
                if is_standalone:
                    string_lines.add(ln)
                else:
                    code_lines.add(ln)
        elif ttype in (tokenize.NEWLINE, tokenize.NL, tokenize.INDENT, tokenize.DEDENT,
                       tokenize.ENCODING, tokenize.ENDMARKER, tokenize.COMMENT):
            pass
        else:
            for ln in range(start[0], end[0] + 1):
                code_lines.add(ln)
        if ttype not in (tokenize.COMMENT, tokenize.NL):
            prev_significant = tok

    string_lines -= code_lines
    comment_lines -= code_lines

    return {
        "total": total,
        "executable": len(code_lines),
        "comment": len(comment_lines),
        "docstring": len(string_lines),
        "_comment_lines": comment_lines,
        "_string_lines": string_lines,
        "_lines": lines,
    }


def marker_lines(path):
    """The narrative-marker measure, over `path`'s comment and docstring lines only.

    Returns a dict with the two quantities kept separate:
      - "carrying": the count of lines whose OWN text matches MARKER_REGEX.
      - "carrying_lines": the set of those line numbers.
      - "expanded": the enclosing-paragraph expansion -- every comment/docstring line in a
        contiguous block that contains at least one carrying line.
      - "expanded_lines": the set of those line numbers.
    """
    info = classify(path)
    lines = info["_lines"]
    narrative_lines = info["_comment_lines"] | info["_string_lines"]

    carrying_lines = {ln for ln in narrative_lines if MARKER_REGEX.search(lines[ln - 1])}

    def block_expand(line_set, flagged_seed):
        expanded = set()
        sorted_lines = sorted(line_set)
        i = 0
        while i < len(sorted_lines):
            j = i
            block = [sorted_lines[i]]
            while j + 1 < len(sorted_lines) and sorted_lines[j + 1] == sorted_lines[j] + 1:
                j += 1
                block.append(sorted_lines[j])
            if any(ln in flagged_seed for ln in block):
                expanded.update(block)
            i = j + 1
        return expanded

    expanded_lines = block_expand(info["_comment_lines"], carrying_lines) | block_expand(
        info["_string_lines"], carrying_lines
    )

    return {
        "carrying": len(carrying_lines),
        "carrying_lines": carrying_lines,
        "expanded": len(expanded_lines),
        "expanded_lines": expanded_lines,
    }


def deletable_partition(path):
    """Marker-carrying lines MINUS those inside a prohibition-adjacent window.

    A line is prohibition-adjacent if it sits within WINDOW lines (either direction) of a line
    matching KEYWORD_REGEX anywhere in the file. This is the denominator AC1 is measured against.
    """
    info = classify(path)
    lines = info["_lines"]
    marker = marker_lines(path)

    keyword_lines = {i + 1 for i, text in enumerate(lines) if KEYWORD_REGEX.search(text)}

    protected = set()
    for kl in keyword_lines:
        for ln in range(kl - WINDOW, kl + WINDOW + 1):
            protected.add(ln)

    deletable = marker["carrying_lines"] - protected
    protected_marker_lines = marker["carrying_lines"] & protected

    return {
        "deletable": len(deletable),
        "deletable_lines": deletable,
        "prohibition_adjacent": len(protected_marker_lines),
        "prohibition_adjacent_lines": protected_marker_lines,
    }
