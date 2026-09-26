"""
coordinator_core.ops.verify_coverage — cross-reference integrity sweep for the
coordinator-claude plugin tree.

Purpose: port of `coordinator/bin/verify-coverage.js` (DoE-claude). Inspired by
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

Port source: coordinator/bin/verify-coverage.js (DoE-claude, 517 lines)
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
    - Item 37 (docs/plans/2026-09-22-inbox-blitz-bundled-xs-s-fixes-2026-09-11.md,
      T37) named four false-orphan legs. Three landed here: line numbers now
      report against the original file (strip_code_fences preserves line
      count instead of deleting stripped regions), a hard-wrapped hyphen
      inside a `<plugin>:<name>` ref no longer truncates the match
      (qualified_re's name group consumes a "-\\n" hard-wrap point), and
      `fork` is in BUILTIN_AGENT_TYPES. The fourth leg -- telling a MENTION
      of a name (prose citing it, not dispatching it) apart from a genuine
      REFERENCE -- is deliberately NOT landed in this pass per the item's own
      body ("spin off the mention/reference leg if it grows past S"); it
      remains open.
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path
from typing import Dict, Iterator, List, Optional, Set, Tuple
from coordinator_core.doe_root_pointer import read_doe_root_pointer_file


_USAGE = (
    "Usage: verify-coverage [--root <path>] [--sweep-root <path>] "
    "[--json] [--report-only]"
)


def parse_args(argv: List[str]) -> dict:
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


def default_root(home_dir: Optional[str] = None) -> str:
    if home_dir is None:
        home_dir = os.path.expanduser("~")
    doe_root = read_doe_root_pointer_file(home_dir)
    if doe_root:
        return doe_root
    return os.path.join(home_dir, ".claude", "plugins", "coordinator-claude")


def default_sweep_root() -> str:
    return os.getcwd()


def discover_artifacts(root: str) -> dict:
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


def strip_code_fences(content: str) -> str:
    out = content
    if out.startswith("---"):
        second_dash = out.find("\n---", 3)
        if second_dash != -1:
            frontmatter = out[:second_dash + 4]
            out = "\n" * frontmatter.count("\n") + out[second_dash + 4:]

    def _blank(m: "re.Match[str]") -> str:
        return "\n" * m.group(0).count("\n")

    out = re.sub(r"```[\s\S]*?```", _blank, out)
    out = re.sub(r"~~~[\s\S]*?~~~", _blank, out)
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
                if entry.name == "worktrees" and os.path.basename(d) == ".claude":
                    continue
                stack.append(full)
            elif entry.is_file() and entry.name.endswith(".md"):
                yield full


_SUBAGENT_RE = re.compile(r"subagent_type\s*[:=]\s*['\"]?([a-z][a-z0-9_:\-]*[a-z0-9])['\"]?")

# with one of these nouns is prose DOCUMENTING a fence/sentinel/marker/block
_MARKER_NOUN_RE = re.compile(r"\b(fence|sentinel|marker|block)s?\b", re.IGNORECASE)

# noun IMMEDIATELY after the closing backtick ("`coordinator:fleet-only`
_MARKER_NOUN_WINDOW_CHARS = 12

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
    2026-08-06-doe-claude-em-verify-coverage-extractor-marker-vocabulary.md):
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

    qualified_re = re.compile(
        r"(?<![\w\-/:.])/?(" + prefix_pattern + r"):"
        r"([a-z](?:-\n(?=[a-z0-9])|[a-z0-9\-])*)(?![\w\-:])"
    )

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

    for m in qualified_re.finditer(body):
        line_num = body.count("\n", 0, m.start()) + 1
        leading_slash = m.group(0).startswith("/")
        ref_name = m.group(2).replace("\n", "")
        if not leading_slash:
            # Only inspect the window immediately TRAILING the matched ref
            trailing_window = body[m.end():m.end() + _MARKER_NOUN_WINDOW_CHARS]
            if _MARKER_NOUN_RE.search(trailing_window):
                continue
        refs.append({
            "kind": "command" if leading_slash else "qualified",
            "ref": f"{m.group(1)}:{ref_name}",
            "line": line_num,
        })
    for m in _SUBAGENT_RE.finditer(body):
        line_num = body.count("\n", 0, m.start()) + 1
        refs.append({"kind": "subagent", "ref": m.group(1), "line": line_num})

    return refs


BUILTIN_AGENT_TYPES: Set[str] = {
    "general-purpose",
    "Explore",
    "Plan",
    "statusline-setup",
    "fork",
}
"""Built-in harness `subagent_type` values with no on-disk artifact -- checked
ahead of the agents-map lookup in `resolve()` for subagent/worker refs."""


def resolve(ref: str, kind: str, artifacts: dict) -> bool:
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
                return True
            return ref in agents
        return any(key.endswith(f":{ref}") for key in agents)

    return False


REF_ALLOWLIST: Set[str] = {
    "coordinator:artifact-consolidation",
    "coordinator:lesson-triage",
    "coordinator:writing-plans",
    "coordinator:requesting-code-review",
    "coordinator:using-git-worktrees",
    "coordinator:doctor",
    "deep-research:doctor",
    "game-dev:schema-migration-auditor",
    "coordinator:fan-out",
    # FORWARD-reference: an unimplemented rename plan proposes this target skill.
    "coordinator:session-complete",
    # FORWARD-reference: draft merge-gate-DoD plans propose this engine op name.
    "coordinator:validate-invocable",
    "coordinator:session-start",
    "coordinator:session-end",
    "coordinator:bootstrap-repos",
    "feature-dev",
    # FORWARD-reference: DoE is authoring this M-tier reviewer (DR-133); claude-klabauter
    # pre-registered its lens in _PLAN_DERIVABLE_LENS so the sidecar files to
    "coordinator:plan-reviewer",
    "coordinator:fleet-only",
    "coordinator:research-",
    "coordinator:inspiration-audit",
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
    # bare-name occurrence flagged here is inside prose DOCUMENTING a failure
    "example-retrieval-repo-context-builder",
    "str",
}


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
    # right one: REF_ALLOWLIST needs a new entry per orphaned REF, so it grows
    # A DELIVERED memo -- inbound under cross-repo/inbox/ or outbound under
    walk_dir_errors: List[str] = []
    for file in walk_markdown(
        sweep_root,
        {
            "dist",
            "review-trail",
            "archive",
            "vendor",
            "audits",
            "subagent-share",
            "tasks",
            "inbox",
            "sent",
            "dispatch-briefs",
            "recovered",
        },
        errors=walk_dir_errors,
    ):
        try:
            with open(file, "r", encoding="utf-8") as fh:
                content = fh.read()
        except (OSError, UnicodeDecodeError) as exc:
            scan_errors.append(f"{file}: {exc}")
            continue
        files_scanned += 1
        refs = extract_references(content, plugins)
        for r in refs:
            if r["ref"] in REF_ALLOWLIST:
                continue
            if not resolve(r["ref"], r["kind"], artifacts):
                violations.append({"file": os.path.relpath(file, sweep_root), **r})

    # INCOMPLETE, not clean -- "ok": true previously meant "no orphans found
    scan_errors = walk_dir_errors + scan_errors
    scan_incomplete = len(scan_errors) > 0

    if args["json"]:
        sys.stdout.write(json.dumps({
            "ok": len(violations) == 0 and not scan_incomplete,
            "scanIncomplete": scan_incomplete,
            "scanErrors": scan_errors,
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
        if scan_incomplete:
            print(f"INCOMPLETE SCAN — {len(scan_errors)} path(s) could not be read:")
            print()
            for e in scan_errors:
                print(f"- {e}")
            print()
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

    if (violations or scan_incomplete) and not args["report_only"]:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
