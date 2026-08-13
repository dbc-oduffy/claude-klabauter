"""
coordinator_core.ops.verify_coverage — cross-reference integrity sweep for the
Coordinator-claude plugin tree.

Purpose: port of `coordinator/bin/verify-coverage.js` (coordinator-claude). Inspired by
Example-game-repo's agent-domain-coverage.test.ts (TOOL_ORPHANED / TOOL_DOUBLE_CLAIMED /
STALE_AGENT_ENTRY against the MCP tool-defs <-> agent-routing-table producer/
consumer contract). This module ports the same shape to coordinator-claude's
producer/consumer surface -- every reference to a skill, agent, or command must
resolve to a real artifact on disk.

Invariants enforced:

    SKILL_ORPHANED   -- every `<plugin>:<skill>` reference resolves to a real
                        <plugin>/skills/<skill>/SKILL.md
    SUBAGENT_ORPHANED -- every `subagent_type: <name>` reference (bare name,
                        `kind="subagent"` in extract_references) resolves to a
                        real <plugin>/agents/<agent>.md OR a known harness
                        built-in agent type (BUILTIN_AGENT_TYPES). Named
                        SUBAGENT_ORPHANED, not AGENT_ORPHANED, in the emitted
                        report -- the human-readable report's section headers
                        are `{kind.upper()}_ORPHANED`, and this reference kind's
                        `kind` value is the string "subagent". A `<plugin>:<agent>`
                        qualified reference that fails to resolve instead falls
                        under QUALIFIED_ORPHANED (kind="qualified"), since a
                        qualified ref is checked against skills/agents/commands
                        together, not agents alone. This docstring previously
                        said "AGENT_ORPHANED", which does not match any code
                        path -- corrected 2026-08-06 per cross-repo memo
                        2026-08-06-example-retrieval-repo-em-verify-coverage-false-positive-orphans.md.
    COMMAND_ORPHANED -- every `/<plugin>:<command>` reference resolves to a real
                        <plugin>/commands/<command>.md. (Bare `/command`, with no
                        plugin-qualified prefix, is NOT actually extracted by
                        `extract_references` -- see Negative-spec below. This
                        docstring line previously over-claimed coverage of that
                        shape; `resolve()`'s bare-ref fallback branch exists but
                        is unreachable from `extract_references`'s output.)
    WORKER_ORPHANED  -- every worker named under a reviewer's "## Worker
                        Dispatch Recommendations" block exists as an agent
                        (special case of AGENT_ORPHANED with a stricter
                        prose-context anchor)

Each reference must resolve to either a skill, an agent, OR a command in the
fully-qualified `<plugin>:<name>` namespace -- the module tracks all three
artifact types under the same prefix because the namespace is shared at the
reference site.

Exit codes (parity-critical -- callers branch on these):
    0 -- all references resolve AND the full sweep tree was readable (or report_only=True)
    1 -- one or more orphan references found, OR the sweep was incomplete (a directory
         or file could not be scanned -- see "scanIncomplete"/"scanErrors" in JSON output)
    2 -- usage / configuration error (unknown flag, missing root/sweep-root dir)

Port source: coordinator/bin/verify-coverage.js (coordinator-claude, 517 lines)
Spec backlink: docs/plans/2026-07-16-clean-slate-recon (BIG_PORT Wave B, item verify-coverage)

Negative-spec (faithful reproduction of the JS oracle's behavior):
    - `--root`/`--sweep-root` existence is checked with plain existence, not
      is-a-directory -- a file path silently "succeeds" through the gate and
      then fails inside discovery/walk with an empty result, exactly as the
      JS oracle does (fs.existsSync makes no directory distinction either).
    - REF_ALLOWLIST is carried over verbatim, including truncated glob-pattern
      entries (e.g. "coordinator:research-") -- these are NOT bugs to fix.
    - Bare `/command` references (no plugin-qualified prefix, e.g. a doc that
      writes `/plan` rather than `/coordinator:plan`) are NEVER extracted as
      references in the first place: the only regex that ever produces
      kind="command" is `qualified_re`, which hard-requires a `<plugin>:`
      prefix before the command name. `resolve()`'s `if ":" not in ref:`
      branch was written to service exactly this bare-command shape but is
      unreachable dead code -- every command-kind `ref` value always contains
      a colon. This is a faithful port: the deleted JS oracle
      (`coordinator/bin/verify-coverage.js`, pre-port SHA `93887f6f^`) has the
      identical `qualifiedRe` shape and the identical never-reached fallback
      in its own `resolve()` -- confirmed by diff, not assumed. A bare
      slash-command with no plugin prefix (e.g. a genuinely nonexistent
      `/totally-made-up-command`) is silently never flagged as
      COMMAND_ORPHANED, despite the module purpose statement above having
      previously implied that shape was covered. Review: code-reviewer --
      flagged as an undocumented scope gap (BIG_PORT Wave B slice
      big-port-wave-b-verify-coverage-refresh-queries, Finding 1); this
      Negative-spec entry + the corrected Invariants wording above are the
      fix (oracle-parity gap, not a port regression).
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path
from typing import Dict, Iterator, List, Optional, Set, Tuple
from coordinator_core.doe_root_pointer import read_doe_root_pointer_file

# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

_USAGE = (
    "Usage: verify-coverage [--root <path>] [--sweep-root <path>] "
    "[--json] [--report-only]"
)


def parse_args(argv: List[str]) -> dict:
    """Parse CLI flags. Mirrors the JS `parseArgs` loop shape (1:1 flag set).

    Returns a dict with keys root/sweep_root/json/report_only, or a dict
    carrying {"_exit": <code>} when --help was requested (exit 0) or an
    unknown argument was seen (exit 2) -- main() checks `_exit` before
    proceeding, replicating the oracle's immediate process.exit() calls
    without exiting the whole interpreter mid-parse (this module is
    direct-imported, not subprocess-invoked).
    """
    args = {"root": None, "sweep_root": None, "json": False, "report_only": False}
    i = 0
    while i < len(argv):
        tok = argv[i]
        if tok == "--root":
            i += 1
            args["root"] = argv[i] if i < len(argv) else None
        elif tok == "--sweep-root":
            i += 1
            args["sweep_root"] = argv[i] if i < len(argv) else None
        elif tok == "--json":
            args["json"] = True
        elif tok == "--report-only":
            args["report_only"] = True
        elif tok in ("-h", "--help"):
            print(_USAGE)
            args["_exit"] = 0
            return args
        else:
            print(f"Unknown argument: {tok}", file=sys.stderr)
            args["_exit"] = 2
            return args
        i += 1
    return args


# ---------------------------------------------------------------------------
# Plugin tree discovery
# ---------------------------------------------------------------------------


def default_root(home_dir: Optional[str] = None) -> str:
    """Resolve the plugin tree root.

    coordinator-claude authoring machines: `~/.claude/.doe-root` contains the absolute path to
    the coordinator-claude clone root. The plugin tree lives directly there (coordinator-claude/
    contains coordinator/, deep-research/, etc. as siblings -- the same shape as
    the published coordinator-claude/ mirror). Reading the sentinel lets this
    module operate against the live authoring tree instead of the publish
    mirror.

    OSS / marketplace installs: sentinel absent -> fall back to the published
    mirror at ~/.claude/plugins/coordinator-claude/.

    `home_dir` is injectable so callers/tests can probe the sentinel logic
    without mutating the real HOME env var (mirrors the JS `defaultRoot`'s
    optional `homeDir` parameter).
    """
    if home_dir is None:
        home_dir = os.path.expanduser("~")
    # `read_doe_root_pointer_file` swallows an unreadable sentinel the same way
    # the prior inline read did (absent sentinel is the expected OSS path), so
    # both the missing and the unreadable case land on the mirror fallback below.
    doe_root = read_doe_root_pointer_file(home_dir)
    if doe_root:
        return doe_root
    return os.path.join(home_dir, ".claude", "plugins", "coordinator-claude")


def default_sweep_root() -> str:
    """Resolve the doc-sweep root (which .md tree gets scanned for references).

    Deliberately distinct from the artifact-discovery root: `discover_artifacts`
    must always resolve the real plugin tree (that's where skills/agents/
    commands live), but the SWEEP -- which files get scanned FOR references --
    must be scoped to whichever repo invoked the module. Defaulting to cwd
    keeps a consumer run scoped to its own doc surface; a coordinator-claude-authoring
    invocation (cwd already inside the resolved plugin root) naturally sweeps
    the plugin tree since cwd IS that tree.
    """
    return os.getcwd()


def discover_artifacts(root: str) -> dict:
    """Walk the plugin tree and discover artifacts.

    The tree shape is: <root>/<plugin>/{skills,agents,commands}/*
    Plus the deep-research subplugin: <root>/deep-research/notebooklm/{skills,agents,commands}/*

    Returns {"skills": {...}, "agents": {...}, "commands": {...}, "plugins": [...]}
    -- skills/agents/commands map "<plugin>:<name>" -> absolute path.
    """
    skills: Dict[str, str] = {}
    agents: Dict[str, str] = {}
    commands: Dict[str, str] = {}

    top_plugins = sorted(
        entry.name
        for entry in os.scandir(root)
        if entry.is_dir() and not entry.name.startswith(".") and entry.name != "docs"
    )

    plugin_pairs: List[Tuple[str, str]] = [(p, os.path.join(root, p)) for p in top_plugins]
    nlm_dir = os.path.join(root, "deep-research", "notebooklm")
    if os.path.exists(nlm_dir):
        plugin_pairs.append(("notebooklm", nlm_dir))

    for plugin, plugin_dir in plugin_pairs:
        skills_dir = os.path.join(plugin_dir, "skills")
        if os.path.exists(skills_dir):
            for entry in sorted(os.scandir(skills_dir), key=lambda e: e.name):
                if not entry.is_dir():
                    continue
                skill_file = os.path.join(skills_dir, entry.name, "SKILL.md")
                if os.path.exists(skill_file):
                    skills[f"{plugin}:{entry.name}"] = skill_file

        agents_dir = os.path.join(plugin_dir, "agents")
        if os.path.exists(agents_dir):
            for entry in sorted(os.scandir(agents_dir), key=lambda e: e.name):
                if not entry.is_file() or not entry.name.endswith(".md"):
                    continue
                name = re.sub(r"\.md$", "", entry.name)
                agents[f"{plugin}:{name}"] = os.path.join(agents_dir, entry.name)

        commands_dir = os.path.join(plugin_dir, "commands")
        if os.path.exists(commands_dir):
            for entry in sorted(os.scandir(commands_dir), key=lambda e: e.name):
                if not entry.is_file() or not entry.name.endswith(".md"):
                    continue
                name = re.sub(r"\.md$", "", entry.name)
                commands[f"{plugin}:{name}"] = os.path.join(commands_dir, entry.name)

    return {
        "skills": skills,
        "agents": agents,
        "commands": commands,
        "plugins": [p for p, _ in plugin_pairs],
    }


# ---------------------------------------------------------------------------
# Reference extraction
# ---------------------------------------------------------------------------


def strip_code_fences(content: str) -> str:
    """Strip fenced code blocks (```...```) and YAML frontmatter from a markdown body.

    Inline backticks (`...`) are KEPT -- many references live in them (e.g.,
    `coordinator:plan`).
    """
    out = content
    if out.startswith("---"):
        second_dash = out.find("\n---", 3)
        if second_dash != -1:
            out = out[second_dash + 4:]
    out = re.sub(r"```[\s\S]*?```", "", out)
    out = re.sub(r"~~~[\s\S]*?~~~", "", out)
    return out


def walk_markdown(
    root: str, exclude: Optional[Set[str]] = None, errors: Optional[List[str]] = None
) -> Iterator[str]:
    """Walk all .md files under a directory tree, depth-first (stack-based,
    matching the JS oracle's `while (stack.length)` shape).

    A directory that fails to scan (permissions, dangling symlink, etc.) is
    silently EXCLUDED from the walk unless `errors` is supplied -- callers that
    need to distinguish "empty subtree" from "unscannable subtree" (i.e. every
    gate consuming this as its scan root) MUST pass a list and check it after
    exhausting the generator.
    """
    if exclude is None:
        exclude = set()
    stack = [root]
    while stack:
        d = stack.pop()
        try:
            entries = list(os.scandir(d))
        except OSError as exc:
            if errors is not None:
                errors.append(f"{d}: {exc}")
            continue
        for entry in entries:
            full = os.path.join(d, entry.name)
            if entry.is_dir():
                if entry.name in ("node_modules", ".git") or entry.name in exclude:
                    continue
                # Path-scoped, not basename-scoped: only .claude/worktrees/* is
                # excluded (untracked worktree checkouts that duplicate every
                # file in the tree). A basename match on ".claude" alone would
                # also exclude tracked .claude fixture dirs under install/
                # sandbox-test surfaces, which are real sweep content.
                if entry.name == "worktrees" and os.path.basename(d) == ".claude":
                    continue
                stack.append(full)
            elif entry.is_file() and entry.name.endswith(".md"):
                yield full


# Pattern 2 -- subagent_type assignments: subagent_type: "name", subagent_type=name.
# Require alphanumeric terminal char so docs templates like "example-game-repo-control:ue-{domain}"
# don't capture the trailing dash before the placeholder brace.
_SUBAGENT_RE = re.compile(r"subagent_type\s*[:=]\s*['\"]?([a-z][a-z0-9_:\-]*[a-z0-9])['\"]?")

# Marker-vocabulary discriminator: a `coordinator:<token>` ref sharing its line
# with one of these nouns is prose DOCUMENTING a fence/sentinel/marker/block
# token, not dispatching it -- see extract_references docstring.
_MARKER_NOUN_RE = re.compile(r"\b(fence|sentinel|marker|block)s?\b", re.IGNORECASE)

# Shape-based proximity bound for the marker-noun discriminator: both of the
# real marker-documentation examples this discriminator was built for put the
# noun IMMEDIATELY after the closing backtick ("`coordinator:fleet-only`
# fence", "`coordinator:percolate-only` sentinel block") -- a genuine
# dispatch reference sharing its line with a marker noun elsewhere in the
# prose ("dispatch `coordinator:foo-worker` to check the marker file") does
# not have that adjacency. Bounding the search window to the text
# immediately trailing the ref (not the whole line) keeps the discriminator a
# shape rule, not a token list, while closing the false-negative the token-
# adjacent shape doesn't share.
_MARKER_NOUN_WINDOW_CHARS = 12

# Worker bullet line, scoped to "## Worker Dispatch Recommendations" blocks.
_WORKER_HEADER_RE = re.compile(r"^##+\s+Worker Dispatch Recommendations", re.IGNORECASE)
_HEADING_RE = re.compile(r"^##+\s")
_WORKER_BULLET_RE = re.compile(r"^\s*[-*]\s+`?([a-z][a-z0-9_\-]+)`?")


def extract_references(content: str, valid_plugin_prefixes: List[str]) -> List[dict]:
    """Extract references from a single markdown file.

    Returns a list of {kind, ref, line} dicts. `kind` is one of:
        'qualified'  -- "<plugin>:<name>" pattern (could be skill/agent/command)
        'subagent'   -- subagent_type: "name" (bare name, requires inference)
        'command'    -- /<name> or /<plugin>:<name> at word boundary
        'worker'     -- name listed under "## Worker Dispatch Recommendations"

    Marker-vocabulary discriminator (2026-08-06, cross-repo memo
    2026-08-06-coordinator-claude-em-verify-coverage-extractor-marker-vocabulary.md):
    `coordinator:` doubles as the fence/sentinel/marker namespace, not only
    the dispatch namespace -- a doc describing a marker TOKEN ("needs a
    `coordinator:fleet-only` fence") is not dispatching anything, and no
    path exclusion can reach live doctrine that documents the vocabulary.
    A bare (non-`/`-prefixed) qualified ref is dropped from `refs` when a
    marker-noun word (fence/sentinel/marker/block, singular or plural)
    appears in the `_MARKER_NOUN_WINDOW_CHARS`-char window immediately
    TRAILING the ref on its line -- not anywhere on the line. Both of the
    sender's real examples put the noun immediately after the closing
    backtick ("`coordinator:fleet-only` fence" / "`coordinator:percolate-only`
    sentinel block"); a same-line-anywhere check also drops a genuine
    dispatch reference whose surrounding prose happens to mention a marker
    noun elsewhere ("dispatch `coordinator:foo-worker` to check the marker
    file"), which is a false-negative risk a bounded trailing window avoids
    (2026-08-06, coordinator-code-reviewer bd2f004c). This is a shape bound
    on the discriminator, not a token list -- it narrows WHERE a marker noun
    must appear relative to the ref, not WHICH words count as marker nouns.
    Scoped to `kind == "qualified"` only: a leading-slash command-form ref
    (`/coordinator:plan`) is a literal invocation syntax even when a marker
    noun happens to share the line, so it is never suppressed by this
    discriminator.
    """
    refs: List[dict] = []
    body = strip_code_fences(content)
    lines = re.split(r"\r?\n", body)

    prefix_pattern = "|".join(p.replace("-", "\\-") for p in valid_plugin_prefixes)

    # Pattern 1 -- Qualified refs: `<plugin>:<name>`. Boundary-strict so URLs and
    # time strings ("12:30") don't match. Allow optional leading slash for command form.
    # Faithful to the JS oracle: an empty prefix_pattern (no plugins discovered)
    # still compiles -- `(...)` with an empty alternation matches an empty string,
    # same behavior new RegExp('()') exhibits in JS.
    qualified_re = re.compile(
        r"(?<![\w\-/:.])/?(" + prefix_pattern + r"):([a-z][a-z0-9\-]+)(?![\w\-:])"
    )

    # Pattern 3 -- worker bullets inside "## Worker Dispatch Recommendations" blocks.
    in_worker_block = False
    for i, line in enumerate(lines):
        if _WORKER_HEADER_RE.match(line):
            in_worker_block = True
            continue
        if in_worker_block and _HEADING_RE.match(line):
            in_worker_block = False
        if in_worker_block:
            m = _WORKER_BULLET_RE.match(line)
            if m:
                refs.append({"kind": "worker", "ref": m.group(1), "line": i + 1})

    # Apply patterns 1 & 2 over the full body.
    for m in qualified_re.finditer(body):
        line_num = body.count("\n", 0, m.start()) + 1
        leading_slash = m.group(0).startswith("/")
        if not leading_slash:
            line_text = lines[line_num - 1]
            # Only inspect the window immediately TRAILING the matched ref on
            # its line (not the whole line) -- both real marker-documentation
            # examples put the noun right after the closing backtick, and
            # bounding the window avoids dropping a genuine dispatch
            # reference whose surrounding prose happens to mention a marker
            # noun elsewhere on the same line (see extract_references
            # docstring). The matched ref's own text is still excluded first
            # so a token that itself contains a marker-noun word (e.g. a
            # hypothetical `coordinator:sentinel-check`) is only suppressed
            # by trailing prose describing it as one, not by its own name.
            match_start_in_line = line_text.find(m.group(0))
            if match_start_in_line != -1:
                trailing_start = match_start_in_line + len(m.group(0))
                trailing_window = line_text[
                    trailing_start:trailing_start + _MARKER_NOUN_WINDOW_CHARS
                ]
            else:
                trailing_window = line_text.replace(m.group(0), "", 1)
            if _MARKER_NOUN_RE.search(trailing_window):
                continue
        refs.append({
            "kind": "command" if leading_slash else "qualified",
            "ref": f"{m.group(1)}:{m.group(2)}",
            "line": line_num,
        })
    for m in _SUBAGENT_RE.finditer(body):
        line_num = body.count("\n", 0, m.start()) + 1
        refs.append({"kind": "subagent", "ref": m.group(1), "line": line_num})

    return refs


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------


BUILTIN_AGENT_TYPES: Set[str] = {
    # Harness-provided agent types that exist by virtue of the Claude Code
    # runtime itself, not a <plugin>/agents/<name>.md file in any plugin tree.
    # No amount of walking the plugin tree will ever produce a backing file
    # for these -- enumeration is the only option, not a stopgap pending a
    # derivation. Halted /update-docs in every consumer repo (not just this
    # one) until allowlisted -- see cross-repo/inbox/2026-08-06-example-retrieval-repo-em-
    # verify-coverage-false-positive-orphans.md item 2.
    "general-purpose",
    "Explore",
    "Plan",
    "statusline-setup",
}
"""Built-in harness `subagent_type` values with no on-disk artifact -- checked
ahead of the agents-map lookup in `resolve()` for subagent/worker refs."""


def resolve(ref: str, kind: str, artifacts: dict) -> bool:
    """Check whether a reference resolves to a registered artifact.

    For qualified refs ("<plugin>:<name>"), check across all three maps
    (skill / agent / command) since the prefix:name namespace is shared at
    the reference site.

    For subagent and worker refs (bare names), check across agents in any
    plugin.

    For command refs ("/<plugin>:<name>" or "/<name>"), check commands first,
    then fall back to skills (since /<name> can invoke a skill).
    """
    skills = artifacts["skills"]
    agents = artifacts["agents"]
    commands = artifacts["commands"]

    if kind == "qualified":
        return ref in skills or ref in agents or ref in commands

    if kind == "command":
        if ref in commands:
            return True
        if ref in skills:
            return True
        if ":" not in ref:
            if any(key.endswith(f":{ref}") for key in commands):
                return True
            if any(key.endswith(f":{ref}") for key in skills):
                return True
        return False

    if kind in ("subagent", "worker"):
        if ref in BUILTIN_AGENT_TYPES:
            return True
        if ":" in ref:
            prefix = ref.split(":")[0]
            known_plugins = {k.split(":")[0] for k in agents.keys()}
            if prefix not in known_plugins:
                # Out-of-tree plugin (e.g. Example-game-repo-control) -- skip silently.
                return True
            return ref in agents
        return any(key.endswith(f":{ref}") for key in agents)

    return False


# ---------------------------------------------------------------------------
# Allowlist for known false positives
# ---------------------------------------------------------------------------

REF_ALLOWLIST: Set[str] = {
    # Historical rename mentions -- skill no longer exists but the documented
    # rename note is the load-bearing artifact (tells future readers where
    # the capability went). Body always reads "Replaces/Supersedes/absorbed".
    "coordinator:artifact-consolidation",  # absorbed into /update-docs Phase 8b 2026-05-06
    "coordinator:lesson-triage",           # renamed to coordinator:learn-lessons 2026-05-06
    # Version-history documentation of renamed skills in super-skill-architecture.md
    # § Version History -- v2.0.0 Breaking Changes (2026-05-07). Not live dispatch refs.
    "coordinator:writing-plans",           # renamed to coordinator:plan 2026-05-07
    "coordinator:requesting-code-review",  # renamed to coordinator:review-code 2026-05-07
    "coordinator:using-git-worktrees",     # removed 2026-05-07 (rule lives in CLAUDE.md)
    # By-design non-command: the coordinator doctor is a wiki + sentinel script,
    # NOT a slash skill.
    "coordinator:doctor",                  # doctor is docs/wiki + sentinel, not a skill (2026-05-20)
    # Documented never-existent artifact: negative example in a schema-required lesson.
    "deep-research:doctor",                # never-existent artifact, cited as negative example (2026-06-17)
    # Project-specific agent in example-game-repo consumer repo, not the global game-dev plugin.
    "game-dev:schema-migration-auditor",   # example-game-repo project-local agent; coordinator wiki cross-ref only
    # Skill demoted to a methodology 2026-05-30 -- collided with native Claude Code vocabulary.
    "coordinator:fan-out",                 # demoted to methodology 2026-05-30
    # FORWARD-reference: an unimplemented rename plan proposes this target skill.
    "coordinator:session-complete",        # forward-ref in unimplemented rename plan (2026-06-01)
    # FORWARD-reference: draft merge-gate-DoD plans propose this engine op name.
    "coordinator:validate-invocable",      # forward-ref, to-be-built op in draft plans (2026-07-22)
    # Renamed to coordinator:workstream-{start,complete}; deprecation-alias stubs deleted 2026-06-01.
    "coordinator:session-start",           # renamed->workstream-start; stub deleted 2026-06-01
    "coordinator:session-end",             # renamed->workstream-complete; stub deleted 2026-06-01
    # historical reference; command retired 2026-06-08.
    "coordinator:bootstrap-repos",         # retired 2026-06-08
    # External installed plugin, NOT part of the coordinator-claude tree; bare-prefix
    # so it bypasses the colon-prefix external-skip path in resolve().
    "feature-dev",                         # external plugin; capability-catalog dispatch-shape doc (2026-06-27)
    # FORWARD-reference: coordinator-claude is authoring this M-tier reviewer (DR-133); claude-klabauter
    # pre-registered its lens in _PLAN_DERIVABLE_LENS so the sidecar files to
    # state/plan-sidecars/ the day it ships. Landing the entry BEFORE the agent
    # exists is the point -- see cross-repo/archive/2026-08-05-coordinator-claude-em-plan-
    # reviewer-lens-registration.md (decision: partial). Drop when DR-133 ships.
    "coordinator:plan-reviewer",           # forward-ref, unshipped coordinator-claude agent DR-133 (2026-08-06)
    # NOT a dispatch target: a publish-boundary fence identifier in DR-248's prose
    # ("`coordinator:fleet-only` fences"). Shares the <plugin>:<name> shape by
    # coincidence of naming, not because anything dispatches it.
    "coordinator:fleet-only",              # publish-boundary fence name, not an agent (2026-08-06)
    # Documented glob PATTERN, not a concrete reference -- trailing `*` gets stripped
    # by the reference parser, yielding this truncated token.
    "coordinator:research-",               # glob pattern for research-* family (2026-07-08)
    # Speculative future-skill name in inspiration-recheck marker prose ("consider
    # extracting ... on the fourth instance"), not a live dispatch reference.
    "coordinator:inspiration-audit",       # proposed-future name in recheck-marker prose (2026-07-19)
    # Historical-record citations of retired/never-built artifacts (coordinator-claude
    # dated 2026-04/2026-05 research docs & plan reviews) -- 2026-07-22, per
    # claude-central-em memo.
    "coordinator:test-driven-development",
    "coordinator:writing-skills",
    "coordinator:verification-before-completion",
    "coordinator:skill-discovery",
    "coordinator:project-onboarding",
    "coordinator:structured-research",
    "coordinator:cockpit",
    "state:open",
    "coordinator:reviewer",
    "schema-migration-auditor",
    "coordinator:hook-doctor",
    # Real artifact (example-retrieval-repo:example-retrieval-repo-context-builder exists), but the
    # bare-name occurrence flagged here is inside prose DOCUMENTING a failure
    # mode ("subagent_type: example-retrieval-repo-context-builder errors with `Agent
    # type not found`" -- docs/wiki/example-retrieval-repo.md), not a dispatch site.
    # Qualifying it there would falsify the quoted error text. Allowlisted
    # rather than teaching the sweep to recognize an inline-code-span-in-prose
    # context (broader parser change, not worth it for one project-local ref).
    "example-retrieval-repo-context-builder",  # documented failure-mode text, not a dispatch site (2026-08-06)
}


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main(argv: List[str]) -> int:
    args = parse_args(argv)
    if "_exit" in args:
        return args["_exit"]

    root = args["root"] or default_root()
    sweep_root = args["sweep_root"] or default_sweep_root()
    if not os.path.exists(root):
        print(f"Plugin root not found: {root}", file=sys.stderr)
        return 2
    if not os.path.exists(sweep_root):
        print(f"Sweep root not found: {sweep_root}", file=sys.stderr)
        return 2

    artifacts = discover_artifacts(root)
    plugins = artifacts["plugins"]

    violations: List[dict] = []
    scan_errors: List[str] = []
    files_scanned = 0
    # Exclude dist/ (generated publish-repo snapshots), review-trail/ (immutable
    # historical code-review findings that deliberately quote wrong refs),
    # archive/ (historical records -- a period-correct ref is not an orphan), and
    # vendor/ (vendored third-party content -- e.g. corpus/vendor/ docs whose
    # type/struct tokens like `state:int32` are not dispatch references).
    #
    # audits/, subagent-share/ and tasks/ carry archive/'s rationale verbatim, not
    # a looser one: an audit record dated 2026-08-01 naming the agent that session
    # actually dispatched stays TRUE when the agent is later retired, and rewriting
    # it to name a live agent would make the record false. Same for a completed
    # dispatch's sidecar and for tasks/ ephemera. Scoping archive/ alone fixed one
    # directory rather than the class, so the gate HALTed /update-docs on 19
    # period-correct references (claude-klabauter, 2026-08-06).
    #
    # Deliberately NOT excluded: the rest of state/ (handoffs, roadmap stubs,
    # improvement-queue) and docs/. Those are live surfaces someone will ACT on --
    # a handoff citing a retired agent is a real orphan, and that is the whole
    # signal this gate exists to produce.
    walk_dir_errors: List[str] = []
    for file in walk_markdown(
        sweep_root,
        # .claude/worktrees/agent-* (untracked worktree checkouts, each
        # duplicating every file in the tree -- 45 found in coordinator-claude, 21
        # were worktree duplicates) is excluded by walk_markdown itself via a
        # path-scoped check, not via this basename exclude set -- a basename
        # exclude on ".claude" would also drop tracked .claude fixture dirs
        # under install/sandbox-test surfaces.
        {"dist", "review-trail", "archive", "vendor", "audits", "subagent-share", "tasks"},
        errors=walk_dir_errors,
    ):
        try:
            with open(file, "r", encoding="utf-8") as fh:
                content = fh.read()
        except (OSError, UnicodeDecodeError) as exc:
            # --- Tier 2 (behaviour change -- PM sign-off required) ---
            scan_errors.append(f"{file}: {exc}")
            # --- end Tier 2 ---
            continue
        files_scanned += 1
        refs = extract_references(content, plugins)
        for r in refs:
            if r["ref"] in REF_ALLOWLIST:
                continue
            if not resolve(r["ref"], r["kind"], artifacts):
                violations.append({"file": os.path.relpath(file, sweep_root), **r})

    # --- Tier 2 (behaviour change -- PM sign-off required) ---
    # A directory or file that could not be scanned means the sweep is
    # INCOMPLETE, not clean -- "ok": true previously meant "no orphans found
    # among whatever happened to be readable," indistinguishable from a real
    # clean result. Fold unscannable-dir errors in here too so they gate the
    # same way as unscannable files.
    scan_errors = walk_dir_errors + scan_errors
    scan_incomplete = len(scan_errors) > 0
    # --- end Tier 2 ---

    if args["json"]:
        sys.stdout.write(json.dumps({
            # --- Tier 2 (behaviour change -- PM sign-off required) ---
            "ok": len(violations) == 0 and not scan_incomplete,
            "scanIncomplete": scan_incomplete,
            "scanErrors": scan_errors,
            # --- end Tier 2 ---
            "root": root,
            "sweepRoot": sweep_root,
            "summary": {
                "skills": len(artifacts["skills"]),
                "agents": len(artifacts["agents"]),
                "commands": len(artifacts["commands"]),
                "filesScanned": files_scanned,
                "violations": len(violations),
            },
            "violations": violations,
        }, indent=2))
        sys.stdout.write("\n")
    else:
        print("# verify-coverage report")
        print()
        print(f"Plugin root: `{root}`")
        print(f"Sweep root: `{sweep_root}`")
        print(f"Plugins: {', '.join(plugins)}")
        print(
            f"Registered: {len(artifacts['skills'])} skills, "
            f"{len(artifacts['agents'])} agents, {len(artifacts['commands'])} commands"
        )
        print(f"Files scanned: {files_scanned}")
        print()
        # --- Tier 2 (behaviour change -- PM sign-off required) ---
        if scan_incomplete:
            print(f"INCOMPLETE SCAN — {len(scan_errors)} path(s) could not be read:")
            print()
            for e in scan_errors:
                print(f"- {e}")
            print()
        # --- end Tier 2 ---
        if not violations:
            print("OK — every reference resolves." if not scan_incomplete else "No orphans found among readable files, but the scan was incomplete (see above).")
        else:
            print(f"Found {len(violations)} orphan reference(s):")
            print()
            by_kind: Dict[str, List[dict]] = {}
            for v in violations:
                by_kind.setdefault(v["kind"], []).append(v)
            for kind in sorted(by_kind.keys()):
                print(f"## {kind.upper()}_ORPHANED ({len(by_kind[kind])})")
                print()
                for v in by_kind[kind]:
                    print(f"- `{v['ref']}`  —  {v['file']}:{v['line']}")
                print()
            print("Fix: add the referenced artifact, or correct the reference. If the reference is")
            print("a known false positive, add it to REF_ALLOWLIST in coordinator_core/ops/verify_coverage.py with a")
            print("one-line rationale.")

    # --- Tier 2 (behaviour change -- PM sign-off required) ---
    if (violations or scan_incomplete) and not args["report_only"]:
        return 1
    # --- end Tier 2 ---
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
