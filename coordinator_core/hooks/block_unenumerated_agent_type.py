"""coordinator_core.hooks.block_unenumerated_agent_type -- PreToolUse(Agent)
hard-deny guard closing the unenumerated-`subagent_type` loophole.

THE HOLE THIS CLOSES. Coordinator's guards and its catering (Bash
confinement, plan-body write confinement, review-sidecar provisioning,
`contract_blocks` doctrine injection, tier/model policy) are all keyed to a
CLOSED, curated roster of agent types. A dispatcher can mint an arbitrary
`subagent_type` at dispatch time, and every one of the eight roster-keyed
sites this plan's census found fails OPEN on a lookup-miss -- an invented
type is simultaneously LESS governed (no Bash confinement, no plan-body
confinement) and LESS instructed (no `do-not-commit`, no guard-encounter
framing) than any agent in the stable. Measured live: a dispatch naming
`hookprobe-named` ran with `permissionMode: bypassPermissions` and zero
catering. See docs/plans/2026-08-10-deny-unenumerated-agent-types-at-dispatch.md
§ Problem / § Why a hard deny.

THE ROSTER IS THE UNION OF THREE SOURCES (AC3) -- NOT the coordinator-
authored set alone. A `coordinator:`-prefixed-only roster is a
self-inflicted fleet outage: it denies every `Explore`, `Plan`,
`general-purpose`, and every plugin agent on first dispatch. Verified at
HEAD (this plan's own review pass): (a)+(b) alone is 100%
`coordinator:`-prefixed and excludes all harness built-ins and 14 plugin
agents.

    (a) the four map keys of example-doctrine-repo's `coordinator/subagent-sandbox-policy.yaml`
        (`report_sidecar`, `report_type_map`, `contract_blocks`,
        `dispatch_tier`) -- resolved via `_load_policy_roster`.
    (b) the agent definitions under example-doctrine-repo's `coordinator/agents/*.md`, keyed
        `coordinator:<name-frontmatter-field>` -- resolved via
        `_load_agents_roster`.
    (c) `_HARNESS_BUILTIN_TYPES` (Anthropic-owned harness built-ins, not
        coordinator-enumerable from (a)/(b) -- these are observed in this
        session's own agent roster, not coordinator doctrine), PLUS plugin
        agents on the discovery path (`~/.claude/plugins/**/agents/*.md`,
        keyed `<plugin-namespace>:<stem>`) -- resolved via
        `_load_plugin_roster`.

FAIL CLOSED, but only on the two PEER-REPO sources ((a)/(b)). An unreadable
or empty example-doctrine-repo-side roster DENIES -- getting this backwards silently restores
the loophole. The plugin half of (c) is LOCAL install state (this machine's
own `~/.claude/plugins/`), not a live peer-repo checkout someone else could
be mid-rebase on, so an absent/unreadable plugins directory degrades to an
EMPTY plugin contribution rather than a roster-load failure -- `_HARNESS_
BUILTIN_TYPES` alone already guarantees the resolved roster is never empty
by construction, so this asymmetry cannot itself trigger the empty-roster
fail-closed leg below.

AC10 -- THE ROSTER-LOAD-FAILURE REASON IS SELF-DESCRIBING. Every `_RosterError`
names the exact path attempted and distinguishes MISSING ENTIRELY (the path
does not exist -- a path/install defect) from PRESENT BUT UNPARSEABLE/
UNREADABLE (an OSError or YAMLError reading a path that exists -- a
peer-repo edit in flight, e.g. mid-rebase on the example-doctrine-repo checkout), so an
operator debugs the two differently without reading source. Deliberately NO
last-good cache as the mitigation -- a stale cache would silently re-open
the loophole for a type deleted from the roster, which is a new pattern
this plan's own anti-scope forbids; the self-describing reason is the whole
mitigation.

DENY ENVELOPE -- copied in shape, not reinvented, from example-doctrine-repo's
`coordinator/hooks/scripts/block-dispatch-suite-invocation.py`
(`_precision_deny_envelope` / its deny writer): `hookSpecificOutput` with
`permissionDecision: "deny"` + `permissionDecisionReason`, exit 0 on the
standalone-script path. This module reuses `coordinator_core._hook_envelope.
deny()`, which builds exactly that shape (plus this repo's own provenance
stamp -- see that module's docstring).

AC2 -- OVERRIDE MARKER. A `COORDINATOR-OVERRIDE-UNENUMERATED-AGENT-TYPE:
<reason>` line, own line, non-empty reason, read off the dispatch prompt --
same pattern as `block-dispatch-suite-invocation.py`'s own
`COORDINATOR-OVERRIDE-DISPATCH-SUITE-GUARD: <reason>` hatch. No new pattern
per PM constraint ("we don't need new patterns here").

INPUT TRUST. `subagent_type`, `name`, and `prompt` are read from `tool_input`
ONLY -- never `dispatched-agents.txt` column 3, which the governed agent can
write.

AC4 -- the deny fires identically whether or not `name` is present; `name`
only changes the deny message's wording, never the decision.

Two entrypoints:
    check(payload) -- pure function, `Dict[str, Any] -> Optional[Dict]`,
        matching this package's own `bash_guards.*` `check()` idiom. Used by
        this repo's tests and by any in-process caller.
    main()          -- stdin-JSON / stdout-JSON / exit-0 standalone script,
        matching example-doctrine-repo's `block-dispatch-suite-invocation.py` calling
        convention, for direct `hooks.json` registration on the Agent
        matcher (example-doctrine-repo-side wiring; out of scope for this chunk).

Spec backlink: docs/plans/2026-08-10-deny-unenumerated-agent-types-at-dispatch.md § C1 / AC1-AC4 / AC10
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Dict, FrozenSet, Optional, Tuple

import yaml

from coordinator_core._hook_envelope import deny
from coordinator_core.doe_root_pointer import read_doe_root_pointer

CLASS = "hard-deny"
MATCHERS = ("Agent",)

#: AC2 -- deliberately hyphenated (own convention, matches the sibling
#: example-doctrine-repo guard's own marker), anchored to line start (leading whitespace
#: allowed), case-sensitive, requires a non-empty (non-whitespace-only)
#: reason. Honored only in `tool_input["prompt"]` -- the text the
#: dispatching caller authored in THIS tool call, never a value read from
#: disk.
_OVERRIDE_MARKER_PREFIX = "COORDINATOR-OVERRIDE-UNENUMERATED-AGENT-TYPE:"
_OVERRIDE_MARKER_RE = re.compile(
    r"^[ \t]*" + re.escape(_OVERRIDE_MARKER_PREFIX) + r"[ \t]*(\S.*)?$",
    re.MULTILINE,
)

#: (c), harness leg -- the Anthropic-owned built-in agent types observed in
#: this session's own agent roster. WHY a frozenset here rather than
#: derived from (a)/(b): these are harness-owned, not coordinator-authored
#: doctrine -- neither `subagent-sandbox-policy.yaml` nor
#: `coordinator/agents/*.md` enumerates them, because coordinator does not
#: own or ship them. Denying a dispatch of any of these is precisely the
#: self-inflicted fleet outage this plan exists to avoid (AC3).
_HARNESS_BUILTIN_TYPES: FrozenSet[str] = frozenset({
    "claude",
    "claude-code-guide",
    "general-purpose",
    "Explore",
    "Plan",
    "statusline-setup",
})

#: (c), plugin leg -- the ONLY top-level `~/.claude/plugins/<entry>`
#: directory name that must never be walked for `agents/*.md`.
#: `_pre-refresh-snapshots` holds pre-refresh backups of plugin trees,
#: including agents deleted from the live plugin -- walking it would
#: resurrect a deleted agent type into the roster. `cache/` and
#: `marketplaces/` were EXCLUDED here before this same-day fix and that was
#: the AC3 regression a coordinator measurement caught live: `cache/` is
#: where most installed plugin agents actually materialize (`installed_
#: plugins.json` almost always points `installPath` at a `cache/<marketplace>/
#: <plugin>/<version>` directory, never the un-cached plugin source), so
#: excluding it silently dropped `example-retrieval-repo:*` and `feature-dev:*` (a
#: Tier-4-named dispatch type) from the roster. `data/` stays excluded only
#: in the sense that it has no `agents/` subtree to find -- walking it is
#: harmless and cheap, so it is no longer special-cased at all.
_PLUGIN_ROSTER_EXCLUDED_TOP_DIRS: FrozenSet[str] = frozenset({
    "_pre-refresh-snapshots",
})

#: The four `subagent-sandbox-policy.yaml` catering maps this plan's own
#: census names (AC3/AC7) -- every key or list-member across all four
#: contributes to roster source (a). `report_sidecar`/`bash_policy` are
#: `list[str]`-or-`dict[str, ...]` shaped depending on section; handled
#: uniformly by `_load_policy_roster` below.
_POLICY_MAP_NAMES = ("report_sidecar", "report_type_map", "contract_blocks", "dispatch_tier")

_FRONTMATTER_RE = re.compile(r"\A---\s*\n(.*?\n)---\s*(?:\n|\Z)", re.DOTALL)

_UNSET = object()


class _RosterError(Exception):
    """Internal signal carrying a self-describing roster-load failure
    reason (AC10) -- never escapes this module; `resolve_roster` catches it
    and turns it into the `(None, reason)` fail-closed return.
    """

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def _extract_frontmatter(text: str) -> Optional[dict]:
    """Return the parsed YAML frontmatter block of `text`, or None if
    `text` carries no `---`-delimited block at its start, or the block does
    not parse to a mapping. Never raises.
    """
    match = _FRONTMATTER_RE.match(text)
    if not match:
        return None
    try:
        data = yaml.safe_load(match.group(1))
    except yaml.YAMLError:
        return None
    return data if isinstance(data, dict) else None


def _load_policy_roster(doe_root: str) -> FrozenSet[str]:
    """Roster source (a) -- the union of all `subagent_type` keys/members
    across `subagent-sandbox-policy.yaml`'s four catering maps.

    Fail-CLOSED (raises `_RosterError`, self-describing per AC10) on: the
    file missing entirely, an OSError reading it, a YAMLError parsing it,
    or a non-mapping top-level document. A per-section shape mismatch (a
    map name present but not a dict/list) is tolerated and simply
    contributes nothing from that one section -- the four maps are
    independently shaped and a malformed one should not blank the other
    three.
    """
    policy_path = Path(doe_root) / "coordinator" / "subagent-sandbox-policy.yaml"
    if not policy_path.is_file():
        raise _RosterError(
            "roster source MISSING ENTIRELY (path/install defect): "
            f"{policy_path} does not exist."
        )
    try:
        raw = policy_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise _RosterError(
            "roster source present but UNREADABLE (peer-repo edit in "
            f"flight?): {policy_path} -- OSError: {exc}"
        ) from exc
    try:
        data = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        raise _RosterError(
            "roster source present but UNPARSEABLE (peer-repo edit in "
            f"flight?): {policy_path} -- YAMLError: {exc}"
        ) from exc
    if not isinstance(data, dict):
        raise _RosterError(
            "roster source present but UNPARSEABLE (top-level YAML "
            f"document is not a mapping): {policy_path}"
        )

    keys: set = set()
    for map_name in _POLICY_MAP_NAMES:
        section = data.get(map_name)
        if isinstance(section, dict):
            keys.update(str(k) for k in section.keys())
        elif isinstance(section, list):
            keys.update(str(v) for v in section)
    return frozenset(keys)


def _load_agents_roster(doe_root: str) -> FrozenSet[str]:
    """Roster source (b) -- `coordinator:<name>` for every `coordinator/
    agents/*.md` frontmatter `name:` field, keyed as the policy file itself
    keys them (confirmed at HEAD: `name: executor` in the frontmatter,
    `coordinator:executor` as the policy map key).

    Fail-CLOSED (raises `_RosterError`, self-describing per AC10) on: the
    directory missing entirely, or an OSError listing it. An individual
    unreadable or frontmatter-less file is skipped, not fatal -- one
    corrupt agent file must not blank the roster for the other 32.
    """
    agents_dir = Path(doe_root) / "coordinator" / "agents"
    if not agents_dir.is_dir():
        raise _RosterError(
            "roster source MISSING ENTIRELY (path/install defect): "
            f"{agents_dir} is not a directory."
        )
    try:
        files = sorted(agents_dir.glob("*.md"))
    except OSError as exc:
        raise _RosterError(
            "roster source present but UNREADABLE (peer-repo edit in "
            f"flight?): {agents_dir} -- OSError: {exc}"
        ) from exc

    names: set = set()
    for md_path in files:
        try:
            text = md_path.read_text(encoding="utf-8")
        except OSError:
            continue
        fm = _extract_frontmatter(text)
        if not fm:
            continue
        name = fm.get("name")
        if isinstance(name, str) and name.strip():
            names.add("coordinator:" + name.strip())
    return frozenset(names)


def _repo_style_plugin_roster(plugins_root: Path) -> "set":
    """Leg 1 -- `~/.claude/plugins/<repo>/<namespace>/agents/*.md`, namespace
    = the directory directly containing the matched `agents/` folder (e.g.
    `game-dev:staff-game-dev`). Walks every top-level entry EXCEPT `cache/`
    and `marketplaces/` (each has its own dedicated, differently-shaped leg
    below -- a generic recursive sweep over them would read their *version*
    or *marketplace* path segment as the namespace, which is wrong) and
    `_pre-refresh-snapshots` (see `_PLUGIN_ROSTER_EXCLUDED_TOP_DIRS`).
    """
    names: set = set()
    try:
        top_entries = sorted(plugins_root.iterdir())
    except OSError:
        return names
    for top in top_entries:
        if (
            top.name in _PLUGIN_ROSTER_EXCLUDED_TOP_DIRS
            or top.name in ("cache", "marketplaces")
            or not top.is_dir()
        ):
            continue
        try:
            agent_files = list(top.glob("**/agents/*.md"))
        except OSError:
            continue
        for md_path in agent_files:
            try:
                rel_parts = md_path.relative_to(top).parts
            except ValueError:
                continue
            if len(rel_parts) < 2 or rel_parts[-2] != "agents":
                continue
            namespace = top.name if len(rel_parts) == 2 else rel_parts[-3]
            names.add(f"{namespace}:{md_path.stem}")
    return names


def _cache_style_plugin_roster(plugins_root: Path) -> "set":
    """Leg 2 -- `~/.claude/plugins/cache/<marketplace>/<plugin>/<version>/
    agents/*.md`, namespace = `<plugin>` (NOT `<version>` -- a plain
    top-relative sweep would read the version hash/tag as the namespace,
    which is the concrete regression measured live: `cache/claude-plugins-
    official/feature-dev/abdaf0fadd68/agents/code-explorer.md` must resolve
    to `feature-dev:code-explorer`, not `abdaf0fadd68:code-explorer`).
    Multiple cached versions of the same plugin (feature-dev alone has 5
    version dirs on the measuring box) all contribute to the same
    `<plugin>` namespace -- a stale extra version's agent set is harmless
    (union, not overwrite), a missing one is the outage this leg exists to
    close.
    """
    names: set = set()
    cache_root = plugins_root / "cache"
    if not cache_root.is_dir():
        return names
    try:
        matches = list(cache_root.glob("*/*/*/agents/*.md"))
    except OSError:
        return names
    for md_path in matches:
        try:
            parts = md_path.relative_to(cache_root).parts
        except ValueError:
            continue
        # (marketplace, plugin, version, "agents", stem.md)
        if len(parts) != 5 or parts[3] != "agents":
            continue
        namespace = parts[1]
        names.add(f"{namespace}:{md_path.stem}")
    return names


def _marketplace_style_plugin_roster(plugins_root: Path) -> "set":
    """Leg 3 -- `~/.claude/plugins/marketplaces/<marketplace>/plugins/
    <plugin>/agents/*.md`, namespace = `<plugin>`.
    """
    names: set = set()
    marketplaces_root = plugins_root / "marketplaces"
    if not marketplaces_root.is_dir():
        return names
    try:
        matches = list(marketplaces_root.glob("*/plugins/*/agents/*.md"))
    except OSError:
        return names
    for md_path in matches:
        try:
            parts = md_path.relative_to(marketplaces_root).parts
        except ValueError:
            continue
        # (marketplace, "plugins", plugin, "agents", stem.md)
        if len(parts) != 5 or parts[1] != "plugins" or parts[3] != "agents":
            continue
        namespace = parts[2]
        names.add(f"{namespace}:{md_path.stem}")
    return names


def _manifest_style_plugin_roster(plugins_root: Path) -> "set":
    """Leg 4 -- the authoritative `installed_plugins.json` manifest
    (`version: 2`): `plugins["<plugin>@<marketplace>"]` -> list of entries
    each carrying `installPath`, a plugin ROOT (not always the `agents/`
    parent -- measured live on the reporting machine (illustrative example
    only, never read at runtime -- abs-path-ok: docstring example, not a
    live path): `example-retrieval-repo@example-retrieval-repo`'s `installPath` was an absolute
    drive-lettered path outside `.claude/plugins`, and its agents lived
    under `<installPath>/plugin/agents/`, one level deeper than
    `<installPath>/agents/`). Namespace = the part of the manifest key
    before `@`. Probes
    BOTH `<installPath>/agents/*.md` and `<installPath>/plugin/agents/*.md`
    per entry.

    Degrades to empty on ANY manifest-read/parse failure -- a malformed or
    unreadable `installed_plugins.json` must NOT deny the fleet; the
    filesystem legs above already cover every install this manifest could
    have named, so losing this one leg costs nothing but a possibly-stale
    extra key it would otherwise have deduplicated for free.
    """
    names: set = set()
    manifest_path = plugins_root / "installed_plugins.json"
    if not manifest_path.is_file():
        return names
    try:
        import json as _json

        manifest = _json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return names
    if not isinstance(manifest, dict):
        return names
    plugins_section = manifest.get("plugins")
    if not isinstance(plugins_section, dict):
        return names

    for manifest_key, entries in plugins_section.items():
        if not isinstance(manifest_key, str) or "@" not in manifest_key:
            continue
        namespace = manifest_key.split("@", 1)[0]
        if not namespace or not isinstance(entries, list):
            continue
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            install_path = entry.get("installPath")
            if not isinstance(install_path, str) or not install_path:
                continue
            install_root = Path(install_path)
            for candidate_agents_dir in (install_root / "agents", install_root / "plugin" / "agents"):
                if not candidate_agents_dir.is_dir():
                    continue
                try:
                    agent_files = list(candidate_agents_dir.glob("*.md"))
                except OSError:
                    continue
                for md_path in agent_files:
                    names.add(f"{namespace}:{md_path.stem}")
    return names


def _load_plugin_roster(home: Optional[str]) -> FrozenSet[str]:
    """Roster source (c), plugin leg -- union of the FOUR distinct layouts
    plugin agents materialize under on this fleet (AC3, same-day fix after
    a coordinator measurement caught two of them missing live):

        1. repo-style   `<repo>/<namespace>/agents/*.md`
        2. cache-style  `cache/<marketplace>/<plugin>/<version>/agents/*.md`
        3. marketplace-style `marketplaces/<marketplace>/plugins/<plugin>/agents/*.md`
        4. manifest     `installed_plugins.json` `installPath` (+ `plugin/` nesting)

    Best-effort at every leg, NEVER raises -- see module docstring "FAIL
    CLOSED, but only on the two PEER-REPO sources": this is local install
    state, an absent/unreadable plugins tree degrades to an empty
    contribution. The manifest leg (4) additionally degrades ALONE on its
    own parse failure without blanking legs 1-3 -- see that function's own
    docstring.

    All four legs are UNIONED, never replace one another: a plugin the
    manifest hasn't caught up with yet is still covered by the filesystem
    legs, and multiple cached versions of the same plugin all contribute
    (a stale extra key is harmless; a missing key is a fleet outage).
    """
    if not home:
        return frozenset()
    plugins_root = Path(home) / ".claude" / "plugins"
    if not plugins_root.is_dir():
        return frozenset()

    names: set = set()
    names |= _repo_style_plugin_roster(plugins_root)
    names |= _cache_style_plugin_roster(plugins_root)
    names |= _marketplace_style_plugin_roster(plugins_root)
    names |= _manifest_style_plugin_roster(plugins_root)
    return frozenset(names)


def _home_dir() -> Optional[str]:
    """Best-effort home-directory resolution for the plugin roster leg --
    `CLAUDE_HOME` first (this repo's established override convention),
    falling back to `HOME`/`USERPROFILE`. Returns None (never "") when
    unresolvable, matching `_load_plugin_roster`'s `if not home` guard.
    """
    return (
        os.environ.get("CLAUDE_HOME")
        or os.environ.get("HOME")
        or os.environ.get("USERPROFILE")
        or None
    )


def _with_override_hint(reason: str) -> str:
    return (
        f"{reason} Override (last resort, clears THIS dispatch only): add "
        f"a line `{_OVERRIDE_MARKER_PREFIX} <reason>` to the dispatch prompt."
    )


def resolve_roster(
    *, doe_root: Any = _UNSET, home: Any = _UNSET
) -> Tuple[Optional[FrozenSet[str]], Optional[str]]:
    """Resolve the union-of-three roster (AC3).

    Returns `(roster, None)` on success, or `(None, reason)` on a
    fail-CLOSED roster-load failure (AC10) -- `reason` is self-describing:
    it names the resolved path(s) actually attempted, the underlying
    OS/parse error verbatim, and the AC2 override marker.

    `doe_root`/`home` are injectable (default: real resolution via
    `read_doe_root_pointer()` / `_home_dir()`) purely for test isolation --
    production callers never pass them.
    """
    if doe_root is _UNSET:
        doe_root = read_doe_root_pointer()
    if home is _UNSET:
        home = _home_dir()

    if not doe_root:
        return None, _with_override_hint(
            "roster source MISSING ENTIRELY (path/install defect): the "
            "example-doctrine-repo root pointer is unresolved -- checked registry "
            "repos.example_doctrine_repo, <settings-home>/machine-local/.doe-root, "
            "${CLAUDE_HOME:-$HOME}/.claude/.doe-root. Sources (a) "
            "coordinator/subagent-sandbox-policy.yaml and (b) "
            "coordinator/agents/*.md cannot be read without it."
        )

    try:
        policy_roster = _load_policy_roster(doe_root)
    except _RosterError as exc:
        return None, _with_override_hint(exc.reason)

    try:
        agents_roster = _load_agents_roster(doe_root)
    except _RosterError as exc:
        return None, _with_override_hint(exc.reason)

    plugin_roster = _load_plugin_roster(home)

    roster = policy_roster | agents_roster | _HARNESS_BUILTIN_TYPES | plugin_roster
    if not roster:
        # Unreachable in practice (`_HARNESS_BUILTIN_TYPES` is a non-empty
        # constant), kept as an explicit fail-closed leg rather than an
        # assumption -- see module docstring "FAIL CLOSED".
        return None, _with_override_hint(
            "roster resolved to the EMPTY SET despite no individual source "
            f"raising -- policy={sorted(policy_roster)!r} "
            f"agents={sorted(agents_roster)!r} "
            f"plugins={sorted(plugin_roster)!r}."
        )
    return roster, None


def _has_override_marker(prompt_text: str) -> bool:
    for match in _OVERRIDE_MARKER_RE.finditer(prompt_text):
        reason = match.group(1)
        if reason and reason.strip():
            return True
    return False


def _unenumerated_deny_reason(subagent_type: str, name: Any) -> str:
    name_str = name.strip() if isinstance(name, str) and name.strip() else None
    named_clause = f"name={name_str!r}" if name_str else "no `name` given"
    return (
        f"AGENT DISPATCH BLOCKED: subagent_type={subagent_type!r} "
        f"({named_clause}) is not on the enumerated roster (union of "
        "(a) example-doctrine-repo coordinator/subagent-sandbox-policy.yaml map keys, "
        "(b) coordinator/agents/*.md, (c) harness built-ins plus "
        "~/.claude/plugins/**/agents/*.md). An unenumerated type gets no "
        "guard coverage and no dispatch catering -- see "
        "docs/plans/2026-08-10-deny-unenumerated-agent-types-at-dispatch.md. "
        "If this is a genuine new agent type, add it to the roster first "
        "(a policy map row and/or coordinator/agents/<type>.md). Override "
        f"(rare -- names the reason): add a line "
        f"`{_OVERRIDE_MARKER_PREFIX} <reason>` to the dispatch prompt."
    )


def check(payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Evaluate the unenumerated-agent-type gate against a
    `PreToolUse(Agent)` payload.

    `subagent_type`/`name`/`prompt` are read from `tool_input` ONLY (see
    module docstring "INPUT TRUST"). A `tool_input` carrying no
    `subagent_type` at all is out of scope for this guard (nothing to
    enumerate against) and passes silently -- that shape is a different
    guard's problem, not this one's.
    """
    if (payload.get("tool_name") or "") not in MATCHERS:
        return None

    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        tool_input = {}

    subagent_type = tool_input.get("subagent_type")
    if not isinstance(subagent_type, str) or not subagent_type.strip():
        return None
    subagent_type = subagent_type.strip()

    prompt = tool_input.get("prompt")
    prompt_text = prompt if isinstance(prompt, str) else ""
    if prompt_text and _has_override_marker(prompt_text):
        return None

    roster, error_reason = resolve_roster()
    if roster is None:
        return deny("PreToolUse", error_reason or "roster unresolved")

    if subagent_type in roster:
        return None

    return deny(
        "PreToolUse",
        _unenumerated_deny_reason(subagent_type, tool_input.get("name")),
    )


def main() -> int:
    """Standalone stdin-JSON / stdout-JSON / exit-0 entrypoint, matching
    example-doctrine-repo's `block-dispatch-suite-invocation.py` calling convention -- see
    module docstring "Two entrypoints".
    """
    try:
        raw = sys.stdin.read()
    except Exception:
        raw = ""
    if not raw:
        return 0
    try:
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            payload = {}
    except Exception:
        payload = {}

    envelope = check(payload)
    if envelope:
        sys.stdout.write(json.dumps(envelope, ensure_ascii=False, separators=(",", ":")))
        sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
