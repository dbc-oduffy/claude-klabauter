"""Standing gate: no shell-pasteable redirect hazard in Python docstrings/comments.

Documentation is a defect vector in an agent-operated repo, in two distinct ways
that this gate closes together because they share one root cause -- describing
Python behavior in shell spelling.

**Hazard 1 -- it executes when pasted.** `coordinator_core/_settings_home.py`
carried, verbatim, a docstring span of the form
`($COORDINATOR_SETTINGS_HOME  ->  ${CLAUDE_HOME:-$HOME}/.coordinator-claude-settings)`.
In a shell `->` is not an arrow: it is `-` followed by the redirect operator `>`.
An agent pasted that span into a shell on 2026-07-28; with CLAUDE_HOME/HOME unset
the redirect target expanded to a bare rooted path and a stray zero-byte file was
created at the current drive root. Sibling artifacts from the same class are still
in this working tree (`3.11` in three directories, from an unquoted `>=3.11`, plus
a scatter of empty prose-word directories).

**Hazard 2 -- it was wrong, and code copied it.** That same docstring described a
`${CLAUDE_HOME:-$HOME}` algorithm the function does not implement (it uses
`Path.home()`, which is load-bearing: `HOME` is a POSIX convention native Windows
shells do not set, so a literal transliteration resolves to the empty string and
yields a cwd-relative settings home). Two sibling resolvers faithfully implemented
the *docstring* and were Windows-broken as a result (fixed in 99a67580 / ee20f22b).

So a docstring in shell spelling is both a paste hazard and a specification the
next author may implement instead of reading the code. The remedy this gate
enforces: describe what the code does, in prose, without `$` expansions and
without `>`.

False-positive strategy
-----------------------
Precision matters more than recall here: a rule that flags every `->` would be
disabled within a week, because Python type hints (`def f() -> Path`), prose
arrows, and legitimate bash documentation all contain one. Three narrowing
mechanisms, in order of how much work they do:

1. **Structural scoping, not pattern exclusion.** Only docstrings (via `ast`) and
   `#` comments (via `tokenize`) are scanned. Real code is never materialized as
   scannable text, so `-> Path` return annotations, `>=` comparisons, and
   generator `>` operators cannot trip the detector no matter how they are
   written. This is the mirror image of `test_no_node_schema_shellout.py`, which
   uses `ast` to exclude docs and scan only code.

2. **Co-occurrence, not a bare operator.** A redirect character alone is not a
   finding. The primary detector fires only when a *shell expansion* (`$VAR`,
   `${VAR}`, `${VAR:-default}`) and a `>` appear together. A docstring arrow with
   no `$` anywhere is prose and is left alone.

3. **Per-line, not per-block.** Detection runs on one physical source line at a
   time -- which is also exactly the threat model, since an agent copies a line or
   a span, not an AST. A `$VAR` in a docstring's first paragraph and a `->` twenty
   lines later are not a finding.

The secondary detector (version constraints) is narrowed further still: it fires
only on a `>=`/`<=` immediately preceding a digit *inside a backticked span*
(`` `python>=3.11` ``), never on unquoted prose like "requires Python 3.11 or
newer".

Suppression
-----------
Genuinely-correct bash documentation is common in this repo and must stay: a
`.sh` file that really does perform `${CLAUDE_HOME:-$HOME}` expansion is correctly
documented in that spelling, and a Python port that mirrors a bash oracle may need
to quote the oracle's actual parameter expansion.

Write `shell-doc-ok: <reason>` anywhere in the same docstring or comment block.
The reason is mandatory -- a bare marker does not suppress -- so every suppression
carries its own justification at the point of use. Block scope (rather than line
scope) is deliberate: it lets the justification read as prose in the surrounding
sentence instead of forcing an inline annotation into the middle of a wrapped
docstring line.

Suppression means "this really is bash". It is not a silencer for a docstring that
describes Python in shell spelling -- that case is a re-render, not an exemption.

shell-doc-ok: this module's own prose quotes the hazardous spellings on purpose --
they are the specimens the gate detects, and rendering them safely would leave the
documentation unable to name what it matches.
"""

from __future__ import annotations

import ast
import io
import re
import tokenize
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]

# Live source trees. Deliberately excludes archive/, state/, tasks/, and
# cross-repo/: those are historical records and dated audit artifacts, and
# re-rendering a record of what was written at the time would falsify it.
_SCAN_ROOTS = ("coordinator_core", "coordinator", "bin", "scripts")

_EXCLUDED_PARTS = frozenset(
    {
        "__pycache__",
        ".venv",
        "venv",
        "node_modules",
        "archive",
        "state",
        "tasks",
        "scratch",
        "scratchpad",
        "cross-repo",
    }
)

# `$VAR`, `${VAR}`, `${VAR:-default}` -- the shell-expansion half of the pair.
_SHELL_EXPANSION_RE = re.compile(r"\$\{[^}\n]*\}|\$[A-Za-z_][A-Za-z0-9_]*")

# The redirect half. A bare `>` is the real hazard character: `->`, `>=`, and
# `>>` are all just `>` with a neighbour as far as the shell lexer is concerned.
_REDIRECT_RE = re.compile(r">")

# Secondary detector: a version floor inside a backticked span.
# shell-doc-ok: the example spellings below are the specimens this detector
# matches; they cannot be rendered safely without losing their meaning.
# Example: `python>=3.11` or `bash >= 4` -- paste-dangerous with no `$` in sight,
# and the source of the `3.11` artifacts still sitting in this working tree.
#
# Deliberately NOT `[<>]=?\s*\d`. That earlier shape produced 67 findings of
# which nearly all were noise, and shipping it would have got the whole gate
# disabled. Two corrections, both grounded in the damage model:
#   - `<` is dropped. Input redirection reads a file; it never creates one. Only
#     `>` can leave an artifact behind, and an artifact is the entire hazard.
#   - `<name>` / `<16hex>` placeholder syntax is pervasive in this repo's
#     docstrings and matched the old shape constantly while being harmless.
# What survives is the version-floor form specifically: `>=` (or `> `) followed
# by a digit, anchored on a preceding identifier or a span start.
_BACKTICK_SPAN_RE = re.compile(r"`([^`\n]+)`")
_VERSION_CONSTRAINT_RE = re.compile(r"(?:^|[A-Za-z0-9_.\-])\s*>=\s*\d")

# The reason must begin with a letter on the marker's own line: a trailing `"""`
# or a lone punctuation mark is not a justification.
_SUPPRESSION_RE = re.compile(r"shell-doc-ok:[ \t]*[A-Za-z]")

# Grandfathered baseline, 2026-07-28. Keyed on (relpath, exact stripped line
# text) -- NEVER a blanket file-level mute. Any OTHER hazard in the same file,
# including a NEW one, still fails the gate. That is the whole point: this is a
# ratchet that freezes the existing population and stops it growing.
#
# Text-keyed rather than line-number-keyed on purpose. These files are actively
# edited, and a lineno key would drift into a false green (silencing a line that
# moved into the exempted number) or a spurious red on every unrelated insertion
# above. Text keys move with their line. The tradeoff is that *editing* an
# exempted line drops it out of the baseline and re-fails the gate -- which is
# the correct direction to fail, since touching the line is exactly when it
# should be re-rendered or justified.
#
# Every entry below was individually classified. The intended end state is that
# this set empties out: class (a) entries get an inline `shell-doc-ok: <reason>`
# at their own site (justification at the point of use beats a central list),
# and the off-limits entries get re-rendered by whoever owns those trees.
#
# GROUP 1 -- off-limits at authoring time. A concurrent session held these trees
# (coordinator_core/install/**, coordinator_core/trusted_root_guard.py); they
# were left unedited to avoid colliding with in-flight work, NOT because they are
# correct. `_shared.py` and both `trusted_root_guard.py` entries are class (b)
# re-renders -- the same `${CLAUDE_HOME:-$HOME}` shape as the original defect.
#
# GROUP 2 -- class (a): genuinely documents a real bash / awk / jq / JSON-Schema
# oracle, where the shell spelling is the accurate one and re-rendering would
# make the documentation wrong. These need a `shell-doc-ok:` marker, not a fix.
_BASELINE: set[tuple[str, str]] = {
    # ---- GROUP 1: off-limits (concurrent session), class (b) re-renders owed --
    ('coordinator_core/install/manifest_reader.py',
     'repo_root resolution order: explicit argument -> $REPO_ROOT env var.'),
    ('coordinator_core/install/_shared.py',
     '4. ``${CLAUDE_HOME:-$HOME}/.doe-root`` pointer file -> ``<repo>/coordinator``.'),
    ('coordinator_core/install/first_run.py',
     'bash>=4.3 (`exec "$_fr_new_bash" "$SELF" --post-toolchain ...`) purely so'),
    ('coordinator_core/install/first_run.py',
     '`<settings-home>/bin/` (a DIFFERENT directory, never `$PLUGIN_ROOT/bin/` --'),
    ('coordinator_core/install/prereq_probe.py',
     'bash oracle\'s `zsh -lc \'printf %s "$PATH"\' 2>/dev/null || true`.'),
    ('coordinator_core/install/run_platform_localize.py',
     '`"$_cc_python" .github/scripts/validate-json-schemas.py 2>&1 | grep -E'),
    ('coordinator_core/install/sandbox_check.py',
     "- The bash oracle's ``bash >= 4`` self-guard has"),
    ('coordinator_core/trusted_root_guard.py',
     'override -> ``${CLAUDE_HOME:-$HOME}/.coordinator-claude-settings``'),
    ('coordinator_core/trusted_root_guard.py',
     '``COORDINATOR_SETTINGS_HOME`` -> ``${CLAUDE_HOME:-$HOME}/.coordinator-claude-settings``'),
    # ---- GROUP 2: class (a), real oracle documentation, owed a marker ---------
    ('coordinator_core/bash_guards/block_subagent_destructive_action.py',
     '# ``"${COORDINATOR_SETTINGS_HOME:-$HOME/.coordinator-claude-settings}/bin/<cli>"``'),
    ('coordinator_core/bash_guards/tests/test_block_subagent_destructive_action.py',
     '# `"${COORDINATOR_SETTINGS_HOME:-$HOME/.coordinator-claude-settings}/bin/<cli>"`'),
    ('coordinator_core/conftest.py',
     "#     version; the repo declares a `>=9.1` floor in pyproject.toml's"),
    ('coordinator_core/contract/cockpit_schema/emit_schema.py',
     'Inline every `$ref: "#/$defs/<Name>"` site with its (recursively'),
    ('coordinator_core/contract/cockpit_schema/emit_schema.py',
     'resolved) `$defs[<Name>]` content. See module docstring\'s "Known'),
    ('coordinator_core/contract/cockpit_schema/emit_schema.py',
     '`$defs` (map of entity-name -> schema) and `required` (a plain list of'),
    ('coordinator_core/hooks/track_dispatched_agents.py',
     '# mirrors `echo "$SESSION_ID" > "$TMP_BP"`'),
    ('coordinator_core/hooks/track_dispatched_agents.py',
     '# Mirrors: printf \'%s\\t%s\\t%s\\t%s\\n\' "$AGENT_ID" "$MODEL" "$SUBAGENT_TYPE" "$(date +%s)" >> "$DISPATCHED"'),
    ('coordinator_core/ops/backfill_deliverable_spine.py',
     "permission preservation) — the bash oracle's `awk ... > $TMPFILE; mv"),
    ('coordinator_core/ops/changelog_ops.py',
     "Returns full file content including trailing newline (matches oracle's {} > $out redirect)."),
    ('coordinator_core/ops/changelog_ops.py',
     "# Mirrors the { echo ...; echo; ...; echo '```' } > $out block exactly."),
    ('coordinator_core/ops/coordinator_complete_entry.py',
     "least XS, exactly as the oracle's ``(( AGENT_DISPATCHES >= 0 ))`` always"),
    ('coordinator_core/ops/cruft_sweep.py',
     '# Mirrors bash `cs_timeout 60 -- rm -rf "$target" 2>/dev/null || true`: a'),
    ('coordinator_core/ops/emit/sections/health.py',
     '``node "$COORDINATOR_ROOT/bin/query-records.js" --type health-status --root <scan_root>'),
    ('coordinator_core/ops/emit/sections/routine_signals.py',
     'In-process replacement for ``_run_staleness`` (was: ``bash "$script" 2>/dev/null ||'),
    ('coordinator_core/ops/learn_lessons_roots.py',
     'mirrors the bash oracle\'s `"$MACHINE_LOCAL" ... 2>/dev/null || true` skip-absent'),
    ('coordinator_core/ops/learn_lessons_roots.py',
     '`awk -F\'|\' \'NF>=4{print $4}\'`."""'),
    ('coordinator_core/ops/name_personas.py',
     "- Does NOT require `perl` or `bash >= 4.0` -- the bash oracle's preflight"),
    ('coordinator_core/ops/percolate_ci_smoke_check.py',
     '`cd "<dest>" && "$PY" .github/scripts/run-all-checks.py`.'),
    ('coordinator_core/ops/percolate_preflight_scratch_publish.py',
     '"""Mirrors the bash oracle\'s `find "$dest_dir" -path \'*<fragment>/*\' -type f`."""'),
    ('coordinator_core/ops/promote_shipped_in_flight_stubs.py',
     '`bash "$ROLLUP" "$deliverable_id" 2>/dev/null || true` — treated as an'),
    ('coordinator_core/ops/release_tagging.py',
     'existing="$(git rev-parse "$TAG" 2>/dev/null || true)"'),
    ('coordinator_core/ops/rollup_derive.py',
     'Mirrors the bash oracle\'s `"$CHECK_SHIPPED" "${RESOLVING_SHAS[@]}" >/dev/null 2>&1`'),
    ('coordinator_core/ops/run_pre_ci_hooks.py',
     '§ Step 5a), which iterated ``$PERCOLATE_ROOT/setup/percolate-hooks/<target>/'),
    ('coordinator_core/ops/run_pre_ci_hooks.py',
     'pre-ci/*.sh`` invoking each via ``bash "$hook" "<dest>"``. That shape has no'),
    ('coordinator_core/ops/validate_install_contract.py',
     '- No `jq` / `bash >= 4.3` runtime precondition: this is a pure-Python'),
    ('coordinator_core/ops/write_identity_file.py',
     '+ `cat > "$_tmp" <<EOF ... EOF` + `mv "$_tmp" ...` shell fence that'),
    ('coordinator_core/orientation/regenerate_cache.py',
     'bash oracle writes via a direct ``printf > "$CACHE_FILE"`` overwrite (NOT'),
    ('coordinator_core/plugin_health/fleet_reachability.py',
     'SETTINGS_HOME:-$HOME/.coordinator-claude-settings}/bin/<name>`) — see'),
    ('coordinator_core/plugin_health/fleet_reachability.py',
     'HOME:-$HOME/.coordinator-claude-settings}/bin/<name>`).'),
    ('coordinator_core/plugin_health/fleet_reachability.py',
     'exclusion -- `${HOME}/bin/<name>` (a recognized non-oracle marker) ends'),
    ('coordinator_core/plugin_health/fleet_reachability.py',
     'generic user-bin path (`~/bin/<name>`, `$HOME/bin/<name>`) — a shebang'),
    ('coordinator_core/plugin_health/fleet_reachability.py',
     '`templates/bin/<name>` / a settings-home `${...}/bin/<name>` expansion'),
    ('coordinator_core/plugin_health/scan.py',
     "# ${CLAUDE_PLUGIN_ROOT}/<path> token extraction. Capture must start with '/'"),
    ('coordinator_core/session/core.py',
     'Parity note: the bash original is ``kill -0 "$pid" 2>/dev/null``,'),
    ('coordinator_core/session/liveness.py',
     '``core.iso_to_epoch``, ``elapsed = now - last`` clamped ``>= 0``,'),
    ('coordinator_core/session/shape.py',
     'Bash. Port of the inline ``mkdir "$lock_dir" && echo ... > ...`` block.'),
    ('coordinator_core/session/tests/test_liveness.py',
     '# my resolves empty AND recorded is non-empty -> False (bash: `-n $my &&`).'),
    ('coordinator_core/session_hierarchy/derive.py',
     '# jq: + if ($session | length) > 0 then {system: {...+created_by_session}} else {} end'),
    ('coordinator/bin/lessons-outbox-drain.py',
     'this script existed, Steps 2/3/4/6 were prose with `<peer-path>`/`<N>`/`${DRAIN_DATE}`'),
    ('coordinator/bin/repo-setup-args-and-register.py',
     'if pip_stderr=$(python3 -m pip install -e "$CLAUDE_PLUGIN_ROOT/whoami/" 2>&1 >/dev/null); then'),
    ('coordinator/bin/repo-setup-args-and-register.py',
     '_cc_claude_klabauter="${REPO_CLAUDE_KLABAUTER:-${CLAUDE_KLABAUTER_ROOT:-<pointer-file lookups>}}"'),
    ('coordinator/bin/repo-setup-args-and-register.py',
     'if machine-local has "repos.$_repo_key" >/dev/null 2>&1; then'),
    ('coordinator/bin/resolve-repo-path.py',
     'mirrors the bash oracle\'s `"$MACHINE_LOCAL" get "$KEY" 2>/dev/null || true`.'),
    ('coordinator/bin/stitch-observer-sidecar.py',
     'Replaces the inline `cat "$OBSERVER_SIDECAR" >> "$DAILY_SUMMARY"; rm'),
    ('coordinator/bin/test_cross_repo_memo_draft.py',
     '"""compose <topic> --open with empty $EDITOR exits 0; stdout includes path + warning.'),
    ('coordinator/bin/tests/file-attribution/test_derive_file_attribution.py',
     't7: Bash echo > $OUTPUT (ambiguous)       → unknown, file_path=None → skipped'),
    ('coordinator/bin/tests/test_agent_helper_shim_doe_corpus_coverage.py',
     '${COORDINATOR_SETTINGS_HOME:-${CLAUDE_HOME:-$HOME}/.coordinator-claude-settings}/bin/<cli>'),
    ('coordinator/bin/tests/test_agent_helper_shim_doe_corpus_coverage.py',
     '2. `CC_BIN="${COORDINATOR_SETTINGS_HOME...}/bin"` assignment, then `"$CC_BIN/<cli>"`.'),
    ('coordinator/bin/tests/test_agent_helper_shim_doe_corpus_coverage.py',
     'own alias name), then `"${_wsc_scc}/<cli>"`.'),
    ('coordinator/bin/validate-install-contract.py',
     '# contract/generator, claude-klabauter owns engine). The `jq` and `bash >= 4.3`'),
    ('coordinator/bin/workday-complete-close.py',
     'invoked them: `python3 "${_mkb_bin}/<cli>.py" ...` with no cd)."""'),
    ('coordinator/bin/workday-complete-reconcile.py',
     '`git add -- "$_ENTRY" 2>/dev/null || true` (staging failure here is'),
    ('coordinator/bin/workday-start-day-branch-resolve.py',
     'printf \'%s  %s\\\\n\' "$(date -u +"%Y-%m-%dT%H:%M:%SZ")" "$REAP_LOG" >> ~/.claude/logs/coordinator-reap.log'),
    ('coordinator/lib/percolate/resolve_target.py',
     '`${meta_root}/plugins/<key>` with a stderr warning, exactly as'),
    ('coordinator/tests/test_draft_plan_aging.py',
     '- `bash "$CLI" ...` -> `subprocess.run([sys.executable, str(CLI), ...])` —'),
    ('coordinator/tests/test_hook_shims_portable.py',
     'bin/ git-hook emitter must be registered) keyed on a bash `cat > "$HOOK"`'),
    ('coordinator/tests/test_percolate_resolve_target.py',
     '${meta_root}/plugins/<key> with a warning (never raises, never consults'),
    ('coordinator/tests/test_verify_doe_root_seam_sync.py',
     '--fix rewrites the `$HOME/.claude/.doe-root` (trailing 2>/dev/null) shape'),
}


def _relpath(path: Path) -> str:
    return path.resolve().relative_to(_REPO_ROOT).as_posix()


def _is_excluded(path: Path) -> bool:
    return any(part in _EXCLUDED_PARTS for part in path.parts)


def _docstring_blocks(tree: ast.AST, lines: list[str]) -> list[list[tuple[int, str]]]:
    """Return each module/class/function docstring as a list of (lineno, text).

    The *source* lines spanned by the docstring node are returned, not the
    parsed string value -- an agent copies what is on screen, and the source
    form is what carries the indentation and quoting the paste would include.
    """
    blocks: list[list[tuple[int, str]]] = []
    doc_owners = (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
    for node in ast.walk(tree):
        if not isinstance(node, doc_owners):
            continue
        body = getattr(node, "body", None)
        if not body:
            continue
        first = body[0]
        if not (
            isinstance(first, ast.Expr)
            and isinstance(first.value, ast.Constant)
            and isinstance(first.value.value, str)
        ):
            continue
        start = first.value.lineno
        end = first.value.end_lineno or start
        blocks.append([(n, lines[n - 1]) for n in range(start, end + 1) if n <= len(lines)])
    return blocks


def _comment_blocks(source: str) -> list[list[tuple[int, str]]]:
    """Return runs of consecutive `#` comment lines as (lineno, text) blocks.

    Consecutive comments are grouped so a `shell-doc-ok:` justification can sit
    on its own line within a multi-line comment, matching docstring behavior.
    """
    blocks: list[list[tuple[int, str]]] = []
    current: list[tuple[int, str]] = []
    prev_row = None
    try:
        tokens = list(tokenize.generate_tokens(io.StringIO(source).readline))
    except (tokenize.TokenError, IndentationError, SyntaxError):
        return blocks
    for tok in tokens:
        if tok.type != tokenize.COMMENT:
            continue
        row = tok.start[0]
        if prev_row is not None and row != prev_row + 1:
            blocks.append(current)
            current = []
        current.append((row, tok.string))
        prev_row = row
    if current:
        blocks.append(current)
    return blocks


def _line_hazards(text: str) -> list[str]:
    """Classify one line of documentation text. Empty list means clean."""
    hazards: list[str] = []
    if _SHELL_EXPANSION_RE.search(text) and _REDIRECT_RE.search(text):
        hazards.append("shell-expansion-with-redirect")
    for span in _BACKTICK_SPAN_RE.findall(text):
        if _VERSION_CONSTRAINT_RE.search(span):
            hazards.append("backticked-version-constraint")
            break
    return hazards


def find_paste_hazards(
    root: Path, repo_root: Path | None = None, apply_baseline: bool = True
) -> list[tuple[str, int, str, str]]:
    """Walk `root` for .py files and return every (relpath, lineno, hazard, text).

    Used both against the real source trees (the standing gate) and against an
    isolated tmp_path fixture (the gate's own self-test, proving it detects
    rather than merely passing by absence).
    """
    repo_root = repo_root or _REPO_ROOT
    findings: list[tuple[str, int, str, str]] = []
    for path in sorted(root.rglob("*.py")):
        if _is_excluded(path.relative_to(root)):
            continue
        try:
            relpath = path.resolve().relative_to(repo_root).as_posix()
        except ValueError:
            relpath = path.resolve().relative_to(root.resolve()).as_posix()
        try:
            source = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        try:
            tree = ast.parse(source, filename=str(path))
        except SyntaxError:
            continue
        lines = source.splitlines()
        blocks = _docstring_blocks(tree, lines) + _comment_blocks(source)
        for block in blocks:
            if any(_SUPPRESSION_RE.search(text) for _, text in block):
                continue
            for lineno, text in block:
                if apply_baseline and (relpath, text.strip()) in _BASELINE:
                    continue
                for hazard in _line_hazards(text):
                    findings.append((relpath, lineno, hazard, text.strip()))
    return findings


def _all_findings() -> list[tuple[str, int, str, str]]:
    findings: list[tuple[str, int, str, str]] = []
    for name in _SCAN_ROOTS:
        root = _REPO_ROOT / name
        if root.is_dir():
            findings.extend(find_paste_hazards(root))
    return findings


def test_no_shell_paste_hazard_in_docstrings_or_comments():
    """Standing gate: live Python docs contain no shell-pasteable redirect.

    A finding here is one of two things. Either the line documents a real bash
    oracle -- then say so with `shell-doc-ok: <reason>` in the same block -- or
    it describes Python behavior in shell spelling, in which case re-render it in
    prose with no `$` and no `>`, after confirming what the code actually does.
    Do not re-render on pattern match alone: these docstrings were wrong about
    their own functions, and a sweep that propagates a wrong description faster
    is a net loss.
    """
    findings = _all_findings()
    rendered = "\n".join(f"  {p}:{n} [{h}] {t}" for p, n, h, t in findings)
    assert findings == [], (
        f"Found {len(findings)} shell-pasteable span(s) in docstrings/comments:\n"
        f"{rendered}"
    )


def test_baseline_contains_no_stale_entries():
    """The grandfathered baseline must shrink, never rot.

    Every entry has to still match a real hazard line. When someone fixes or
    suppresses a baselined site, its entry goes stale and this test names it for
    deletion -- so the set cannot quietly accumulate mutes for lines that no
    longer exist, and its size stays an honest count of the remaining debt.
    """
    live = {
        (relpath, text)
        for name in _SCAN_ROOTS
        if (_REPO_ROOT / name).is_dir()
        for relpath, _lineno, _hazard, text in find_paste_hazards(
            _REPO_ROOT / name, apply_baseline=False
        )
    }
    stale = sorted(entry for entry in _BASELINE if entry not in live)
    rendered = "\n".join(f"  {p}\n    {t}" for p, t in stale)
    assert stale == [], (
        f"{len(stale)} baseline entr(ies) no longer match any hazard line — the "
        f"site was fixed, suppressed, or moved. Delete them from _BASELINE:\n"
        f"{rendered}"
    )


def test_gate_detects_the_original_settings_home_regression(tmp_path):
    """The exact 2026-07-28 span, replanted. Without this the gate would only be
    passing by absence and could not prove it catches a reintroduction."""
    fixture = tmp_path / "fixture_settings_home.py"
    fixture.write_text(
        '"""Resolve the settings home.\n'
        "\n"
        "($COORDINATOR_SETTINGS_HOME -> ${CLAUDE_HOME:-$HOME}/.coordinator-claude-settings)\n"
        '"""\n'
        "\n"
        "def settings_home():\n"
        "    return None\n",
        encoding="utf-8",
    )

    findings = find_paste_hazards(tmp_path)

    assert len(findings) == 1
    relpath, lineno, hazard, _text = findings[0]
    assert relpath.endswith("fixture_settings_home.py")
    assert lineno == 3
    assert hazard == "shell-expansion-with-redirect"


def test_gate_detects_a_planted_comment_hazard(tmp_path):
    """`#` comments are scanned too -- the hazard is the paste, not the quoting."""
    fixture = tmp_path / "fixture_comment.py"
    fixture.write_text(
        "# Resolution order: $COORDINATOR_MACHINE -> machine-local registry.\n"
        "VALUE = 1\n",
        encoding="utf-8",
    )

    findings = find_paste_hazards(tmp_path)

    assert len(findings) == 1
    assert findings[0][1] == 1
    assert findings[0][2] == "shell-expansion-with-redirect"


def test_gate_detects_a_backticked_version_constraint(tmp_path):
    """The `>=3.11` sibling class: paste-dangerous with no `$` present.

    shell-doc-ok: the fixture below must contain the literal hazardous spelling.
    """
    fixture = tmp_path / "fixture_version.py"
    fixture.write_text('"""Requires `python>=3.11` at minimum."""\n', encoding="utf-8")

    findings = find_paste_hazards(tmp_path)

    assert len(findings) == 1
    assert findings[0][2] == "backticked-version-constraint"


def test_gate_ignores_code_prose_and_type_hints(tmp_path):
    """Negative control -- the exact noise this gate must NOT flag.

    Return annotations in real code and quoted in docstrings, `>=` comparisons in
    real code, prose arrows with no shell expansion, a `$VAR` with no redirect
    anywhere near it, and an unquoted prose version requirement.
    """
    fixture = tmp_path / "fixture_benign.py"
    fixture.write_text(
        '"""Module doc.\n'
        "\n"
        "Public surface:\n"
        "    def settings_home() -> Path: ...\n"
        "    def machine_local_dir() -> Path: ...\n"
        "\n"
        "Enrichment runs review -> execute -> verify in order.\n"
        "Reads $COORDINATOR_SETTINGS_HOME when it is set.\n"
        "Requires Python 3.11 or newer.\n"
        '"""\n'
        "from pathlib import Path\n"
        "\n"
        "# Falls back to the platform home when the override is absent.\n"
        "def settings_home(n: int = 0) -> Path:\n"
        "    if n >= 3:\n"
        "        return Path('/')\n"
        "    return Path.home()\n",
        encoding="utf-8",
    )

    findings = find_paste_hazards(tmp_path)

    assert findings == []


def test_suppression_requires_a_reason(tmp_path):
    """A bare marker does not suppress; `shell-doc-ok: <reason>` does."""
    bare = tmp_path / "bare"
    bare.mkdir()
    (bare / "fixture.py").write_text(
        '"""Mirrors the oracle\'s `${rest##* -> }` expansion. shell-doc-ok:"""\n',
        encoding="utf-8",
    )
    assert len(find_paste_hazards(bare)) == 1

    justified = tmp_path / "justified"
    justified.mkdir()
    (justified / "fixture.py").write_text(
        '"""Mirrors the oracle\'s `${rest##* -> }` expansion.\n'
        "\n"
        "shell-doc-ok: quotes the bash original's real parameter expansion.\n"
        '"""\n',
        encoding="utf-8",
    )
    assert find_paste_hazards(justified) == []


def test_suppression_is_block_scoped_not_file_scoped(tmp_path):
    """A justification in one docstring must not silence a hazard in another."""
    fixture = tmp_path / "fixture_two_blocks.py"
    fixture.write_text(
        '"""Mirrors `${rest##* -> }`.\n'
        "\n"
        "shell-doc-ok: quotes the bash original's real parameter expansion.\n"
        '"""\n'
        "\n"
        "def resolve():\n"
        '    """Resolution order: $COORDINATOR_MACHINE -> registry."""\n'
        "    return None\n",
        encoding="utf-8",
    )

    findings = find_paste_hazards(tmp_path)

    assert len(findings) == 1
    assert findings[0][1] == 7


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
