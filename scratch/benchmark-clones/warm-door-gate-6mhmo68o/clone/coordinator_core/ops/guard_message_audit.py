"""coordinator_core.ops.guard_message_audit -- the deterministic hitlist of
every guard emit site whose agent-visible text hands the reader a mechanism.

PURPOSE. Four sibling repos independently reported coordinator's own guard text
to us as suspected prompt injection (example-retrieval-repo and example-market-data-repo,
2026-08-11; example-cockpit-repo, 2026-08-13). Each report was actioned by editing the
one leg it named, and the defect returned on a different leg every time. This
module exists so the question "which emit sites hand an agent a key?" has a
DETERMINISTIC, RE-RUNNABLE answer rather than an agent-derived one -- a survey
that missed a site is how this recurred four times.

Spec backlink: state/sizings/2026-08-13-guard-advisories-read-as-prompt-injectio.yaml.
Reports it answers: cross-repo/inbox/2026-08-13-example-cockpit-repo-em-guard-advisories-read-as-injection-to-subagents.md,
state/audits/2026-08-11-guard-text-injection-mechanism-proof.md.

THE RULE THIS ENCODES (PM ruling, 2026-08-13):
  - A dispatched subagent must NEVER see an unlock/override mechanism exist --
    no key, no path, no command, no env var, no recipe, and no wiki pointer
    either. A confined agent does not get the keys to its own cage.
  - The EM is routed to a wiki page describing the locks; the key is not printed
    into the deny message.
  - Where audience cannot be resolved, degrade to the terse form. Do NOT fall
    back to emitting the mechanism.

This INVERTS the fail direction recorded in the 2026-08-11 audit, which resolved
an unresolvable agent identity toward EMIT ("absent agent_id means the main/EM
session where a human is watching"). An implementer who inherits that branch
unchanged has reintroduced the defect.

WHY THE GUARDS EXIST AT ALL, because it determines the fail direction above:
these are not defences against a malicious attacker. They stop amnesiac Claudes
from machine-degrading or otherwise deleterious behaviour. The realistic failure
is therefore a WELL-MEANING subagent rationalising its way through the gate --
"my EM told me to run the full test suite, and this guard says test runs need
this button pressed, so I press it." Showing a confined agent the key is what
makes that rationalisation available; withholding it makes the gate hold without
depending on doctrine the agent may not recall.

NEGATIVE SPEC -- WHAT THIS MODULE IS NOT.
  - NOT a security boundary and not a linter that blocks. It REPORTS. The
    blocking artifact is a test that consumes this module's rows; keeping the
    enumeration separate from the assertion is what lets the same rows drive a
    census, a CI gate, and an ad-hoc query without three enumerations drifting
    apart.
  - NOT hand-maintained. Every row is derived from a live registry or a
    filesystem walk. A hand-listed guard set would be exactly the "rule someone
    remembers" this work exists to replace -- if you find yourself adding a
    literal guard name to a list in this file, the enumeration seam is wrong.
  - NOT audience-resolving by itself. `audience_aware` reports whether a module
    CONSULTS an audience-resolution seam, never whether it does so correctly.
    A True there is a prompt to go read the branch, not a pass.

STATIC, DELIBERATELY. Detection is a source scan, not a render sweep: rendering
every guard's message requires a synthetic payload per guard that satisfies each
one's own detection predicate, which is per-guard bespoke work that would itself
rot. A source scan over-reports (a token in a docstring or a test fixture counts)
and that is the correct direction for a hitlist -- a false positive costs one
read, a false negative is a missed site, which is the failure this module exists
to prevent. Rows carry `evidence` so a reader can dismiss a docstring hit in
seconds.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

#: Packages whose modules can produce agent-visible text. Derived, not guessed:
#: every directory under `coordinator_core/` containing a non-test module that
#: mentions a hook-output text field (see `TEXT_FIELDS`). Recomputed by
#: `discover_emitting_packages()` rather than frozen here, so a new guard family
#: is picked up without editing this module.
TEXT_FIELDS = (
    "permissionDecisionReason",
    "additionalContext",
    "systemMessage",
)

#: Agent-visible text is NOT only the hook envelope. A CLI that refuses and
#: explains itself on stderr reaches an agent's context exactly as a deny does,
#: and reads the same way. Found the hard way: the first version of this module
#: scanned `TEXT_FIELDS` alone and MISSED
#: `ops/ceremony/scoped_git_commit._CLAIM_CONFLICT_REMEDY`, which names a guard
#: key inline, carries an unresolvable cross-repo doc path, and closes with the
#: "doctrine violation, not a shortcut" register -- a site that appears in no
#: prior memo, audit, or sizing precisely because every previous survey also
#: looked only at guard envelopes.
#:
#: A module qualifies on this axis when it writes to stderr AND carries the
#: unlock/override vocabulary. Both conditions, because either alone floods:
#: most of the package writes to stderr about something, and the vocabulary
#: appears in plenty of modules that render no text at all.
STDERR_MARKERS = (
    "stderr",
    "sys.stderr",
)
#: HIGH-SIGNAL ONLY, and the tightening is the whole design of this axis.
#: A first cut matched bare "override"/"unlock"/"sentinel" anywhere in source
#: and returned 347 sites, 261 of them from this axis -- docstrings in modules
#: that render nothing dominated, and a hitlist that reports a third of the
#: engine has stopped being a hitlist. These are phrases that appear in text
#: WRITTEN FOR A READER, not in prose about the mechanism: each one is a thing a
#: message says TO someone, which is exactly the genre being hunted.
UNLOCK_VOCABULARY = (
    "guard-unlock",
    "doctrine violation, not a shortcut",
    "human-only affordance",
    "override key",
    "override keys",
    "-prefixed prompt",
    "bypasses this hook",
    "bypasses pretooluse",
    "unblock (",
    "cannot be granted by this agent",
)

#: Source-level signatures of "this text hands the reader a mechanism".
#: Each entry is (category, compiled pattern). Categories are the vocabulary the
#: fix slate is organised by, so keep them stable.
MECHANISM_PATTERNS: Tuple[Tuple[str, "re.Pattern[str]"], ...] = (
    ("env-override-key", re.compile(r"\bCOORDINATOR_[A-Z0-9_]*OVERRIDE[A-Z0-9_]*\b")),
    ("env-override-key", re.compile(r"\b[A-Z][A-Z0-9_]{3,}=1\b")),
    ("shell-unlock-command", re.compile(r"\btouch\s+\S*\{|\btouch\s+/|\btouch\s+<")),
    ("shell-unlock-command", re.compile(r"\bexport\s+[A-Z][A-Z0-9_]+=")),
    ("marker-path", re.compile(r"allow-xrepo-write")),
    ("marker-path", re.compile(r"\.git/[a-z0-9-]*(allow|grant|bypass|disarm)[a-z0-9-]*")),
    ("sentinel-name", re.compile(r"\b[a-z0-9-]*sentinel[a-z0-9-]*\b", re.IGNORECASE)),
    ("cli-invocation", re.compile(r"python3?\s+-m\s+coordinator_core")),
    ("cli-invocation", re.compile(r"PYTHONPATH=")),
    ("doc-pointer", re.compile(r"docs/(wiki|reference)/\S+\.md")),
    ("doc-pointer", re.compile(r"coordinator-claude-settings\S*")),
    ("unlock-exists-statement", re.compile(r"in-session unlock|unlock exists|override exists|bypass", re.IGNORECASE)),
)

#: Source-level signatures that a module CONSULTS an audience-resolution seam.
#: Presence means "go read this branch", never "this module is correct".
AUDIENCE_SEAMS: Tuple[Tuple[str, "re.Pattern[str]"], ...] = (
    ("agent-class", re.compile(r"\bresolve_agent_class\b|\bAGENT_CLASS_SUBAGENT\b")),
    ("subagent-identity", re.compile(r"\bresolve_subagent_identity\b")),
    ("effective-session", re.compile(r"\beffective_session_id\b")),
    ("raw-agent-id", re.compile(r"\bagent_id\b")),
)

#: Register violations -- the apologetic/self-legitimating tells. Doctrine:
#: docs/wiki/guard-messaging.md § Register, docs/wiki/message-register-doctrine.md.
REGISTER_PATTERNS: Tuple[Tuple[str, "re.Pattern[str]"], ...] = (
    ("self-legitimacy", re.compile(r"this engine's own|genuine|legitimate|not an attack|first-party", re.IGNORECASE)),
    ("apology", re.compile(r"\bsorry\b|\bapolog", re.IGNORECASE)),
    ("reassurance", re.compile(r"don't worry|no need to worry|rest assured|this is safe", re.IGNORECASE)),
    ("deniability", re.compile(r"cannot be granted by this agent|not a shortcut|human-only", re.IGNORECASE)),
)


@dataclass
class EmitSite:
    """One module that can produce agent-visible text."""

    module: str
    package: str
    guard_names: List[str] = field(default_factory=list)
    bands: List[str] = field(default_factory=list)
    registry: str = "unregistered"
    text_fields: List[str] = field(default_factory=list)
    mechanism_hits: List[Dict[str, str]] = field(default_factory=list)
    audience_seams: List[str] = field(default_factory=list)
    register_hits: List[Dict[str, str]] = field(default_factory=list)

    @property
    def audience_aware(self) -> bool:
        return bool(self.audience_seams)

    @property
    def verdict(self) -> str:
        """The single word a fix slate sorts on.

        Ordering is by remediation cost, worst first: a site that hands over a
        mechanism AND cannot tell who is reading is the one that ships a key to a
        confined subagent today.
        """
        if self.mechanism_hits and not self.audience_aware:
            return "HANDS-KEY-TO-ANYONE"
        if self.mechanism_hits:
            return "HANDS-KEY-AUDIENCE-SPLIT-EXISTS"
        if self.register_hits:
            return "REGISTER-ONLY"
        return "CLEAN"


def _scan(text: str, patterns: Sequence[Tuple[str, "re.Pattern[str]"]]) -> List[Dict[str, str]]:
    """Every distinct (category, matched-fragment) pair in `text`.

    Deduplicated on the pair, so a token repeated forty times in one module
    contributes one row -- a hitlist that reports occurrence counts buries the
    site list it exists to produce.
    """
    seen: set = set()
    hits: List[Dict[str, str]] = []
    for category, pattern in patterns:
        for match in pattern.finditer(text):
            fragment = match.group(0).strip()
            key = (category, fragment)
            if key in seen:
                continue
            seen.add(key)
            hits.append({"category": category, "evidence": fragment})
    return hits


def _iter_source_modules(root: Path) -> Iterable[Path]:
    """Every non-test, non-cache Python module under `root`.

    Tests are excluded because the enforcement corpus is a separate census with a
    different question (what does CI currently REQUIRE?) -- conflating them
    produces a hitlist where half the rows cannot be fixed by editing a message.
    """
    for path in sorted(root.rglob("*.py")):
        parts = path.parts
        if "__pycache__" in parts or "tests" in parts:
            continue
        if path.name.startswith("test_"):
            continue
        # This module names every forbidden token as pattern data, so it matches
        # its own scan. Excluded as a reporting tool rather than an emit site --
        # it composes no agent-visible text. Stated rather than left as a
        # mystery row, because a self-hit in a hitlist reads as a real finding.
        if path.name == "guard_message_audit.py":
            continue
        yield path


def _bash_guard_registry() -> Dict[str, Tuple[str, str]]:
    """`{module_stem: (guard_name, band)}` from the LIVE bash_guards chain.

    Fail-open to `{}` on any import/introspection error: this module is a
    reporting tool, and a registry it cannot read degrades the rows to
    `registry="unregistered"` rather than taking the audit down. A partial
    hitlist is still a hitlist; a crashed one is a survey nobody ran.
    """
    out: Dict[str, Tuple[str, str]] = {}
    try:
        from coordinator_core.bash_guards import dispatch as _dispatch

        chain = _dispatch._build_guard_chain("true", "", ".", {}, None, None)
    except Exception:
        return out
    for entry in chain or ():
        name = getattr(entry, "name", None)
        band = getattr(getattr(entry, "band", None), "value", None) or str(getattr(entry, "band", ""))
        if not name:
            continue
        # Registration names are hyphenated (`bump-foreign-repo-write`); module
        # stems are underscored (`bump_foreign_repo_write.py`). Key on the
        # normalized stem so the join actually lands -- without this the whole
        # bash_guards half of the hitlist reads `unregistered`, which is the
        # silent-miss failure this tool exists to prevent.
        out[name.replace("-", "_")] = (name, band)
    return out


def _write_guard_registry() -> Dict[str, Tuple[str, str]]:
    """`{module_stem: (guard_name, class)}` from write_guards' live discovery.

    Same fail-open contract as `_bash_guard_registry`.
    """
    out: Dict[str, Tuple[str, str]] = {}
    try:
        from coordinator_core.write_guards import engine as _engine

        guards, _failed = _engine._discover_guards()
    except Exception:
        return out
    for guard in guards or ():
        name = getattr(guard, "name", None)
        cls = getattr(guard, "cls", "")
        if not name:
            continue
        out[name] = (name, str(cls))
    return out


def collect(repo_root: Path) -> List[EmitSite]:
    """Every emit site under `coordinator_core/`, scanned and classified."""
    core = repo_root / "coordinator_core"
    bash_reg = _bash_guard_registry()
    write_reg = _write_guard_registry()

    sites: List[EmitSite] = []
    for path in _iter_source_modules(core):
        try:
            source = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        present_fields = [f for f in TEXT_FIELDS if f in source]
        if not present_fields:
            # Second axis -- a CLI that refuses on stderr and explains the
            # unlock. See STDERR_MARKERS' comment for why this is not optional.
            writes_stderr = any(m in source for m in STDERR_MARKERS)
            has_vocab = any(v in source.lower() for v in UNLOCK_VOCABULARY)
            if not (writes_stderr and has_vocab):
                continue
            present_fields = ["stderr"]

        rel = path.relative_to(repo_root).as_posix()
        stem = path.stem
        package = path.parent.relative_to(repo_root).as_posix()

        registry = "unregistered"
        guard_names: List[str] = []
        bands: List[str] = []
        if stem in bash_reg:
            registry = "bash_guards.dispatch"
            guard_names, bands = [bash_reg[stem][0]], [bash_reg[stem][1]]
        elif stem in write_reg:
            registry = "write_guards.engine"
            guard_names, bands = [write_reg[stem][0]], [write_reg[stem][1]]

        sites.append(
            EmitSite(
                module=rel,
                package=package,
                guard_names=guard_names,
                bands=bands,
                registry=registry,
                text_fields=present_fields,
                mechanism_hits=_scan(source, MECHANISM_PATTERNS),
                audience_seams=[c for c, p in AUDIENCE_SEAMS if p.search(source)],
                register_hits=_scan(source, REGISTER_PATTERNS),
            )
        )
    return sites


def _as_rows(sites: Sequence[EmitSite]) -> List[Dict[str, object]]:
    rows = []
    for site in sites:
        row = asdict(site)
        row["audience_aware"] = site.audience_aware
        row["verdict"] = site.verdict
        rows.append(row)
    return rows


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="guard-message-audit",
        description="Deterministic hitlist of guard emit sites that hand the reader a mechanism.",
    )
    parser.add_argument("--repo-root", default=".", help="repo root (default: cwd)")
    parser.add_argument("--json", action="store_true", help="emit JSON rows instead of a table")
    parser.add_argument(
        "--verdict",
        action="append",
        help="filter to one or more verdicts (repeatable): HANDS-KEY-TO-ANYONE, "
        "HANDS-KEY-AUDIENCE-SPLIT-EXISTS, REGISTER-ONLY, CLEAN",
    )
    parser.add_argument("--category", action="append", help="filter to sites with a mechanism hit in this category")
    parser.add_argument("--package", help="filter to one package path prefix")
    parser.add_argument(
        "--fail-on-hit",
        action="store_true",
        help="exit 1 if any row survives the filters -- the CI-gate mode",
    )
    args = parser.parse_args(argv)

    root = Path(args.repo_root).resolve()
    sites = collect(root)

    if args.verdict:
        wanted = {v.upper() for v in args.verdict}
        sites = [s for s in sites if s.verdict in wanted]
    if args.category:
        wanted_cat = set(args.category)
        sites = [s for s in sites if any(h["category"] in wanted_cat for h in s.mechanism_hits)]
    if args.package:
        sites = [s for s in sites if s.package.startswith(args.package)]

    if args.json:
        json.dump(_as_rows(sites), sys.stdout, indent=2)
        sys.stdout.write("\n")
    else:
        by_verdict: Dict[str, List[EmitSite]] = {}
        for site in sites:
            by_verdict.setdefault(site.verdict, []).append(site)
        for verdict in (
            "HANDS-KEY-TO-ANYONE",
            "HANDS-KEY-AUDIENCE-SPLIT-EXISTS",
            "REGISTER-ONLY",
            "CLEAN",
        ):
            group = by_verdict.get(verdict, [])
            if not group:
                continue
            print("\n== %s (%d)" % (verdict, len(group)))
            for site in group:
                cats = sorted({h["category"] for h in site.mechanism_hits})
                print(
                    "  %-72s reg=%-22s audience=%s %s"
                    % (
                        site.module,
                        site.registry,
                        ",".join(site.audience_seams) or "none",
                        ("hits=" + ",".join(cats)) if cats else "",
                    )
                )
        print("\ntotal emit sites: %d" % len(sites))

    if args.fail_on_hit and sites:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
