"""
coordinator_core.ops.verify_subagent_sandbox_preamble_sync — check/fix/list
the `subagent-sandbox-preamble` sentinel block across the scoped-agent
CONSUMER ROLE MAP (scouts/specialists/workers/checkers/auditors only — NOT
Opus personas, NOT executor/review-integrator/enricher/docs-checker).

PER-ROLE ADAPTATION, NOT A BLANKET IN/OUT LIST (C5, 2026-07-24). The old
shape was a single flat `_CONSUMER_RELATIVE_PATHS: List[str]` synced
byte-for-byte against ONE canonical body — every consumer, regardless of
what kind of provisioned doc it actually gets, saw the identical generic
"write your working notes and output there" prose. That is exactly the
blanket scratch-offer the citizenship rollout (docs/plans/2026-07-24-
agent-citizenship-identity-adapted-provisioning.md, C1-C4) was built to
retire: `subagent-sandbox-policy.yaml`'s `report_type_map:` now stamps each
eligible subagent_type with its own template type (review-findings /
assessment / run-report / staff-eng-review), and the canonical snippet
(`snippets/subagent-sandbox-preamble.md`) carries one `<!-- VARIANT:<type>
--> ... <!-- END VARIANT -->` block per template type actually used by this
module's cohort. `_CONSUMER_ROLE_MAP: Dict[str, str]` (relative agent path
-> variant/template type) replaces the flat list: each consumer is synced
against ITS OWN variant body, not the one shared body every prior consumer
saw. No home-owning agent in this cohort is left pointed at a generic
scratch pad — every entry names its own typed doc.

CLI contract preserved verbatim from the bash original
(coordinator/bin/verify-subagent-sandbox-preamble-sync.sh):
    verify-subagent-sandbox-preamble-sync.sh          verify (default) — non-zero on drift.
    verify-subagent-sandbox-preamble-sync.sh --check  alias for default mode (explicit).
    verify-subagent-sandbox-preamble-sync.sh --fix    insert/rewrite sentinel blocks to match canon.
    verify-subagent-sandbox-preamble-sync.sh --list   print one consumer path per line, exit 0.

Sentinel pair (exact strings):
    <!-- BEGIN subagent-sandbox-preamble (synced from snippets/subagent-sandbox-preamble.md) -->
    <!-- END subagent-sandbox-preamble -->

Exit codes (fail-loud drift-gate script — a claude-klabauter-link/transport failure is
handled entirely by the DoE-side trampoline BEFORE this module is reached,
and uses a dedicated code, 3, that collides with none of the codes below —
see the trampoline's own comment block):
    0 — clean (verify/fix mode: no MISSING/MISMATCH/MISSING_END/MISSING_FILE
        rows) or --list mode (always 0 once past the CLI-usage checks below).
    1 — drift found: at least one consumer is MISSING_FILE (regardless of
        mode — nothing to insert into a file that doesn't exist), or, in
        verify mode only, MISSING / MISSING_END / MISMATCH.
    2 — CLI-usage / environment error: unknown mode argument (the `node`
        not-on-PATH case retired with the node subprocess — see negative-spec
        below).

Port of: verify-subagent-sandbox-preamble-sync.sh (DoE b5a4192c, 2026-07-20;
         255 lines)
Spec backlink: DoE-claude:pln-bash-polyglot-clean-slate-full-5c71ee

Not a JSON-RPC op — a plain module, NOT @register_op'd, called by direct
import from the DoE-side polyglot trampoline (template-variant #1, mirrors
coordinator-auto-push / handoff-gate-aging / verify_templates_bin_sync).

Negative-spec (deliberate divergences / faithfully-reproduced oracle shape):
    - The bash oracle's `PYTHON_BIN="$(command -v python3 || command -v
      python || true)"` presence check is DROPPED, not faithfully
      reproduced — it existed only to find an interpreter for two
      `python3 -c <<'PYEOF'` heredocs (insert_block/rewrite_block). Now that
      this whole module IS the Python process (the polyglot trampoline
      already resolved and is running under python3/python/py before this
      module is ever imported), that presence check is structurally
      unreachable dead code post-port, not a behavior this module could
      still exercise — dropping it is not a scope-drop of user-visible
      behavior.
    - The `node` presence check is DROPPED (2026-07-22 sentinel-blocks-cli.js
      dependency port): extract_block used to shell out to `node
      lib/sentinel-blocks-cli.js extract` for lack of a ported extraction
      primitive; `coordinator_core.text.sentinel_blocks.extract_block` (a
      byte-parity port of `coordinator/bin/lib/sentinel-blocks.js`, already
      landed and used by `coordinator_core/text/sentinel_blocks_cli.py`) now
      does the same string-slice op in-process, so there is no subprocess to
      require `node` on PATH for. This retires an entire class of
      environment-dependency (node-missing) exit-2 failure this module could
      previously hit — a strict behavior improvement, not a scope-drop: the
      extracted block content is byte-identical (both implementations do
      exact-substring marker lookup + line-boundary consumption, see
      `sentinel_blocks.py`'s own docstring for the shared contract).
    - insert_block/rewrite_block are ported as native Python (no more
      `python3 -c <<'PYEOF'` subprocess indirection) — this is a
      behavior-preserving simplification (same process, same code, one
      fewer subprocess hop), not a divergence in observable output.
    - CONSUMERS-array ordering, insertion-anchor logic (prefer
      quota-self-detect-preamble's END sentinel; else the LAST adjacent END
      sentinel found; else before the first "## Examples" heading; else
      end-of-file), and the has-BEGIN / has-END / block-normalize-compare
      sequence are byte-parity ports of the bash/awk/python-heredoc
      original.
    - PLUGIN_ROOT / SCRIPT_DIR resolution (CLAUDE_PLUGIN_ROOT env override,
      else the trampoline's own parent directory) is DoE-repo topology
      knowledge the trampoline resolves and passes in as this module's
      first two positional args — mirrors verify_templates_bin_sync.py's
      plugin_root-as-argv[0] precedent. COORDINATOR_CONTENT_ROOT is a plain
      env var read directly in this module (not topology-specific in the
      same way), matching the oracle's own `${COORDINATOR_CONTENT_ROOT:-$PLUGIN_ROOT}`.
"""

from __future__ import annotations

GENERATES = []  # writes into a fixed list of DoE-claude coordinator/agents/*.md consumer files under coord_root (COORDINATOR_CONTENT_ROOT) -- a different repo, never a path inside claude-klabauter's own tree

import os
import re
import sys
from collections import OrderedDict
from pathlib import Path
from typing import List, Optional, Tuple

from coordinator_core.session.declared_writes import declare_write
from coordinator_core.text.sentinel_blocks import extract_block as _extract_block_str

BEGIN_SENTINEL = (
    "<!-- BEGIN subagent-sandbox-preamble "
    "(synced from snippets/subagent-sandbox-preamble.md) -->"
)
END_SENTINEL = "<!-- END subagent-sandbox-preamble -->"

# Scoped-agent Sonnet/Haiku single-job workers only: pipeline scout/specialist/worker
# prompts and checker/auditor agent prompts in coordinator/agents/. Explicitly EXCLUDED
# (per brief): Opus personas (staff-eng, staff-data-sci, senior-front-end, staff-ux,
# eng-director, vp-product, code-architect), executor, review-integrator, enricher,
# docs-checker, and the Agent-Teams sweep/synthesizer roles (research-sweep,
# research-synthesizer, structured-synthesizer, parallel-review-synthesizer). This is a
# citizenship boundary, not a scratch-access boundary: excluded roles already have their
# own protocol-defined typed homes (sidecar, flight-recorder, escalation report) wired
# in by their own agent prompts, so the per-role provisioned-home-or-scratch-fallback
# offer in the canonical snippet body would be redundant noise for them, not a missing
# grant.
#
# VALUE is the variant/template type each path is synced against (matches
# subagent-sandbox-policy.yaml's report_type_map: for that subagent_type) — NOT a
# blanket in/out list any more. Every findings-and-verdict checker/auditor gets the
# review-findings variant; every scout/specialist/read-and-answer worker gets the
# assessment variant. Keys mirror the bash oracle's CONSUMERS array verbatim, same
# order; only the value (str path -> (path, type) pair) changed shape.
_CONSUMER_ROLE_MAP: "OrderedDict[str, str]" = OrderedDict(
    [
        ("agents/code-reviewer.md", "review-findings"),
        ("agents/code-reviewer-weekly.md", "review-findings"),
        ("agents/coverage-auditor.md", "assessment"),
        ("agents/dep-cve-auditor.md", "review-findings"),
        ("agents/doc-link-checker.md", "assessment"),
        ("agents/security-audit-worker.md", "review-findings"),
        ("agents/test-evidence-parser.md", "review-findings"),
        ("agents/prior-art-checker.md", "assessment"),
        ("agents/plan-coverage-checker.md", "review-findings"),
        ("agents/external-pattern-checker.md", "assessment"),
        ("agents/research-scout.md", "assessment"),
        ("agents/research-specialist.md", "assessment"),
        ("agents/research-worker.md", "assessment"),
        ("agents/repo-scout.md", "assessment"),
        ("agents/repo-specialist.md", "assessment"),
        ("agents/notebooklm-research-scout.md", "assessment"),
    ]
)

#: Variant/template types this cohort actually uses (subset of claude-klabauter's
#: provision_report.TEMPLATE_TYPES — run-report/staff-eng-review consumers are
#: excluded from this cohort entirely, see module docstring).
VARIANT_TYPES: Tuple[str, ...] = ("review-findings", "assessment")

_QUOTA_END_PATTERN = re.compile(r"^\s*<!--\s*END\s+quota-self-detect-preamble\b")
_END_PATTERN = re.compile(r"^\s*<!--\s*END\s+(?!subagent-sandbox-preamble)")
_EXAMPLES_PATTERN = re.compile(r"^\s*##\s+(Examples?|example)", re.IGNORECASE)


def _consumers(coord_root: str) -> List[Tuple[str, str]]:
    """Join each `_CONSUMER_RELATIVE_PATHS` entry (forward-slash literals) onto
    `coord_root` as a real filesystem path — NOT a wire id, so this deliberately
    renders with the host's native `os.sep` (backslash on Windows), matching how
    every downstream consumer (Path.is_file, Path.read_text, insert_block/
    rewrite_block's Path(file_path)) and the CLI's stdout rows are expected to look
    on that host. `os.path.join(coord_root, "agents/code-reviewer.md")` is WRONG
    here: it concatenates coord_root and the rel string with one os.sep, but the
    rel string's own embedded "/" separators are left un-converted, producing a
    mixed backslash/forward-slash path that fails identity comparisons against a
    pure-pathlib-built path on Windows. Splitting each rel entry on "/" and handing
    the segments to Path(...) lets pathlib render every separator natively."""
    return [
        (str(Path(coord_root, *rel.split("/"))), variant_type)
        for rel, variant_type in _CONSUMER_ROLE_MAP.items()
    ]


def _import_normalize_snippet():
    """In-process import of the already-ported normalize_snippet — this module and
    normalize_snippet both live in coordinator_core, so a direct import is strictly
    cheaper than the bash oracle's `"$SCRIPT_DIR/normalize-snippet"` subprocess hop
    (same reasoning as coordinator-auto-push's direct-import-over-cc_invoke choice) —
    behavior-preserving, not a divergence in observable output."""
    from coordinator_core.text.normalize_snippet import normalize_snippet

    return normalize_snippet


def _variant_markers(variant_type: str) -> Tuple[str, str]:
    """The `<!-- VARIANT:<type> -->` / `<!-- END VARIANT -->` marker pair the
    canonical snippet uses to delimit one role-adapted body per template type.
    Reuses the same exact-substring marker-slice primitive
    (`coordinator_core.text.sentinel_blocks.extract_block`) as the outer
    consumer-file BEGIN/END sentinel — this is the per-role analog inside the
    single canonical snippet file, not a new extraction mechanism."""
    return f"<!-- VARIANT:{variant_type} -->", "<!-- END VARIANT -->"


def _extract_snippet_variant_body(snippet_text: str, variant_type: str) -> str:
    """Extract the body of ONE `<!-- VARIANT:<variant_type> -->` block from the
    canonical snippet file's raw text (the file carries one such block per
    template type this cohort uses — see `VARIANT_TYPES`). Raises ValueError if
    the named variant block is absent, so a `_CONSUMER_ROLE_MAP` entry pointing
    at an unauthored variant fails loud at verify-time rather than silently
    falling through to a stale/blank body."""
    begin, end = _variant_markers(variant_type)
    result = _extract_block_str(snippet_text, begin, end)
    if result is None:
        raise ValueError(
            f"canonical snippet has no {begin!r} ... {end!r} block for variant "
            f"{variant_type!r}"
        )
    return result["block"].rstrip("\n")


def _has_begin_sentinel(text: str) -> bool:
    """True iff some line, stripped of leading/trailing whitespace, equals the BEGIN
    sentinel exactly. Mirrors the oracle's awk gsub-trim + exact-match check."""
    for line in text.splitlines():
        if line.strip() == BEGIN_SENTINEL:
            return True
    return False


def extract_block(file_path: str) -> Tuple[int, str, str]:
    """Extract the `BEGIN_SENTINEL`/`END_SENTINEL` block body from `file_path`,
    in-process — byte-parity replacement for the retired `node
    lib/sentinel-blocks-cli.js extract <file> <begin> <end>` subprocess spawn
    (2026-07-22 sentinel-blocks-cli.js dependency port; see module
    negative-spec). Delegates the actual marker-slice logic to
    `coordinator_core.text.sentinel_blocks.extract_block`, itself a
    byte-parity port of `coordinator/bin/lib/sentinel-blocks.js`'s
    `extractBlock` (DoE-claude `coordinator/bin/lib/sentinel-blocks.js:80-89`,
    `findMarkers` at :28-68) — same exact-substring marker lookup and
    line-boundary consumption, no regex.

    Returns (returncode, stdout, stderr), mirroring the retired CLI's
    contract: 0 with the block body on success; 1 with "" and a diagnostic
    if the file can't be read or either marker is absent (the CLI's own exit
    code for both failure modes — see `sentinel-blocks-cli.js:39-52`)."""
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            content = f.read()
    except OSError as exc:
        return 1, "", f"sentinel-blocks-cli: cannot read file: {file_path}: {exc}"

    result = _extract_block_str(content, BEGIN_SENTINEL, END_SENTINEL)
    if result is None:
        return (
            1,
            "",
            f"sentinel-blocks-cli: markers not found in {file_path}\n"
            f"  begin: {BEGIN_SENTINEL}\n"
            f"  end:   {END_SENTINEL}",
        )

    return 0, result["block"], ""


def insert_block(file_path: str, body: str) -> None:
    """Insert a sentinel block into a consumer file that does not yet have it.

    Placement: immediately after the END of quota-self-detect-preamble if present
    (the common trailing preamble across all consumers); otherwise after the LAST
    adjacent sentinel block found (not the first — a file may carry an earlier,
    unrelated sentinel block such as text-only-recovery-preamble near the top, and
    anchoring on the first hit would land this block ahead of
    quota-self-detect-preamble instead of alongside it). If no adjacent sentinel
    block exists at all, insert before the first "## Examples" heading, else append
    at end of file. Byte-parity port of the oracle's embedded python heredoc."""
    fpath = Path(file_path)
    block = BEGIN_SENTINEL + "\n" + (body if body.endswith("\n") else body + "\n") + END_SENTINEL + "\n"

    lines = fpath.read_text(encoding="utf-8").splitlines(keepends=True)

    insert_after = -1
    for i, line in enumerate(lines):
        if _QUOTA_END_PATTERN.match(line):
            insert_after = i
            break

    if insert_after < 0:
        for i, line in enumerate(lines):
            if _END_PATTERN.match(line):
                insert_after = i

    if insert_after >= 0:
        out = lines[: insert_after + 1] + ["\n", block] + lines[insert_after + 1 :]
    else:
        insert_before = len(lines)
        for i, line in enumerate(lines):
            if _EXAMPLES_PATTERN.match(line):
                insert_before = i
                break
        out = lines[:insert_before] + [block, "\n"] + lines[insert_before:]

    fpath.write_text("".join(out), encoding="utf-8", newline="\n")
    # DR-276: declared AFTER the write lands, never before — the contract is
    # a report of what was ACTUALLY written, not of an intended surface.
    declare_write(fpath)


def rewrite_block(file_path: str, body: str) -> None:
    """Rewrite an existing sentinel block body in-place (content between BEGIN and
    END). Byte-parity port of the oracle's embedded python heredoc."""
    fpath = Path(file_path)
    lines = fpath.read_text(encoding="utf-8").splitlines(keepends=True)
    out: List[str] = []
    in_block = False
    for line in lines:
        stripped = line.rstrip("\r\n")
        if stripped == BEGIN_SENTINEL:
            out.append(line)
            out.append(body if body.endswith("\n") else body + "\n")
            in_block = True
            continue
        if stripped == END_SENTINEL:
            in_block = False
            out.append(line)
            continue
        if not in_block:
            out.append(line)

    fpath.write_text("".join(out), encoding="utf-8", newline="\n")
    declare_write(fpath)


def run(
    plugin_root: str,
    script_dir: str,
    mode: str,
    coord_root: Optional[str] = None,
) -> Tuple[int, List[str], List[str]]:
    """Core check/fix/list logic. Returns (exit_code, stdout_lines, stderr_lines).

    `coord_root` defaults to the `COORDINATOR_CONTENT_ROOT` env var (else
    `plugin_root`), matching the oracle's `${COORDINATOR_CONTENT_ROOT:-$PLUGIN_ROOT}`.
    """
    stdout_lines: List[str] = []
    stderr_lines: List[str] = []

    snippet_file = Path(plugin_root) / "snippets" / "subagent-sandbox-preamble.md"
    if not snippet_file.is_file():
        stderr_lines.append(f"ERROR: canonical snippet not found at {snippet_file}")
        return 1, stdout_lines, stderr_lines

    if mode not in ("--check", "--fix", "--list"):
        stderr_lines.append(f"ERROR: unknown argument '{mode}'")
        return 2, stdout_lines, stderr_lines

    if coord_root is None:
        coord_root = os.environ.get("COORDINATOR_CONTENT_ROOT") or plugin_root
    consumers = _consumers(coord_root)

    if mode == "--list":
        for consumer, _variant_type in consumers:
            stdout_lines.append(consumer)
        return 0, stdout_lines, stderr_lines

    # Review: code-reviewer — guard against OSError (permission/race-deleted) and
    # UnicodeDecodeError (non-UTF-8 content) so this fail-loud gate script stays on the
    # documented 0/1/2 exit-code table instead of propagating a raw traceback.
    try:
        snippet_text = snippet_file.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        stderr_lines.append(f"ERROR: could not read canonical snippet {snippet_file}: {exc}")
        return 2, stdout_lines, stderr_lines

    normalize_snippet = _import_normalize_snippet()

    # Per-role adaptation (C5): each variant type gets its OWN normalized body and
    # write-ready text, extracted from its own `<!-- VARIANT:<type> -->` block in the
    # canonical snippet — replacing the old single shared snippet_norm/snippet_write.
    # A variant block missing from the canonical snippet is a fail-loud usage error
    # (mode 2), not a silent per-consumer skip: it means the snippet author added a
    # role to _CONSUMER_ROLE_MAP without authoring its offer text.
    variant_norm: dict = {}
    variant_write: dict = {}
    for variant_type in VARIANT_TYPES:
        try:
            body = _extract_snippet_variant_body(snippet_text, variant_type)
        except ValueError as exc:
            stderr_lines.append(f"ERROR: {exc}")
            return 2, stdout_lines, stderr_lines
        norm = normalize_snippet(body)
        variant_norm[variant_type] = norm
        variant_write[variant_type] = norm + "\n"

    exit_code = 0

    for consumer, variant_type in consumers:
        snippet_norm = variant_norm[variant_type]
        snippet_write = variant_write[variant_type]

        cpath = Path(consumer)
        if not cpath.is_file():
            stderr_lines.append(f"MISSING_FILE {consumer}")
            exit_code = 1
            continue

        try:
            text = cpath.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            stderr_lines.append(f"ERROR: could not read {consumer}: {exc}")
            exit_code = 2
            continue
        has_begin = _has_begin_sentinel(text)

        if not has_begin:
            if mode == "--fix":
                insert_block(consumer, snippet_write)
                stdout_lines.append(f"INSERTED     {consumer}")
            else:
                stdout_lines.append(f"MISSING      {consumer}")
                exit_code = 1
            continue

        if END_SENTINEL not in text:
            stdout_lines.append(f"MISSING_END  {consumer}")
            exit_code = 1
            continue

        _rc, block_content, _err = extract_block(consumer)
        block_norm = normalize_snippet(block_content)

        if block_norm == snippet_norm:
            stdout_lines.append(f"OK           {consumer}")
        else:
            if mode == "--fix":
                rewrite_block(consumer, snippet_write)
                stdout_lines.append(f"FIXED        {consumer}")
            else:
                stdout_lines.append(f"MISMATCH     {consumer}")
                exit_code = 1

    return exit_code, stdout_lines, stderr_lines


def main(argv: List[str]) -> int:
    """CLI entry: argv[0]=plugin_root, argv[1]=script_dir, argv[2] (optional)=mode.

    plugin_root/script_dir are DoE-repo topology the trampoline resolves (mirrors
    verify_templates_bin_sync.py's plugin_root-as-argv[0] precedent); mode defaults to
    "--check", matching the oracle's `MODE="${1:---check}"`.
    """
    if len(argv) < 2:
        print(
            "verify-subagent-sandbox-preamble-sync.sh: missing required "
            "plugin_root/script_dir arguments",
            file=sys.stderr,
        )
        return 2
    plugin_root = argv[0]
    script_dir = argv[1]
    mode = argv[2] if len(argv) > 2 else "--check"

    exit_code, stdout_lines, stderr_lines = run(plugin_root, script_dir, mode)
    for line in stdout_lines:
        print(line)
    for line in stderr_lines:
        print(line, file=sys.stderr)
    return exit_code


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
