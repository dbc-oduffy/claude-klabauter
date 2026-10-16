"""coordinator_core.ops._research_contract — research-by-value-class vocabulary as data.

Purpose: one home for the value classes, appetites, sources, seed kinds, depths, tier table,
pipeline names and deep-roster roles shared by research.shape, the research emit route and the
chain composer. Pure: no I/O, no op registration.

Negative-spec: applies no tier logic (research_shape does); authors no manifest or template.
"""

from __future__ import annotations

from typing import Final, TypedDict

VALUE_CLASSES: Final = ("scouts", "corpus", "deep", "spike")
APPETITES: Final = ("small", "medium", "large")
SOURCES: Final = ("web", "repo", "structured", "notebooklm")
# research.seeds[].kind -> the source (pipeline) a seed of that kind feeds. paper -> web is an
# inference: no destination was named for papers.
SEED_KINDS: Final = ("video", "doc", "repo", "paper")
SEED_KIND_SOURCE: Final[dict[str, str]] = {
    "repo": "repo",
    "video": "notebooklm",
    "doc": "web",
    "paper": "web",
}
DEPTHS: Final = ("standard", "deeper", "deepest")
TIERS: Final = ("scouts", "corpus", "deep")
# local-only: every output stays under machine-local research.local_root, outside every repo.
DESTINATIONS: Final = ("repo", "local-only")

# TIER_TABLE[value_class][appetite] -> tier: small moves down a notch, large up, clamped.
TIER_TABLE: Final[dict[str, dict[str, str]]] = {
    "scouts": {"small": "scouts", "medium": "scouts", "large": "corpus"},
    "corpus": {"small": "scouts", "medium": "corpus", "large": "deep"},
    "deep": {"small": "corpus", "medium": "deep", "large": "deep"},
    # spike is DoE's binary-verdict class (procedure: the spike skill, run by the EM); the engine
    # has no spike pipeline, so it takes the corpus row.
    "spike": {"small": "scouts", "medium": "corpus", "large": "deep"},
}

# A research block whose `sources` name any of these is raised from scouts to corpus. A scout
# question's repo/local target is read by that scout and does not floor the tier.
FLOOR_SOURCES: Final = frozenset({"repo", "structured", "notebooklm"})

SCOUTS_PIPELINE: Final = "scouts"
DEEP_PIPELINE: Final = "unblock"
PREFLIGHT_PIPELINE: Final = "nlm-preflight"

# What each pipeline is for, named in the shape reason: `deep` resolves to the unblock
# diagnostic, which a comprehension ask must not run by mistake.
PIPELINE_JOBS: Final[dict[str, str]] = {
    SCOUTS_PIPELINE: "quick web scouts over the ask",
    "web": "web corpus survey",
    "repo": "repo corpus survey",
    "structured": "structured-data corpus survey",
    "notebooklm": "NotebookLM corpus survey",
    DEEP_PIPELINE: "the unblock-us diagnostic (diagnose, decompose, challenge) for an EM stuck "
    "delivering, not a comprehension survey; use --research-class corpus to understand a subject",
}

# Deep roster: role members carry the role in DoE's templates, not in a persona.
UNBLOCK_ROLES: Final = ("diagnose", "decompose", "challenge")
UNBLOCK_ROLE_AGENT_TYPE: Final = "general-purpose"

# One specialist per source in the deep roster; sources absent here get none.
SOURCE_SPECIALIST: Final[dict[str, str]] = {
    "web": "coordinator:research-specialist",
    "repo": "coordinator:repo-specialist",
}

# The bound is load: every scout is one more Sonnet LLM on a box already running ~50.
MAX_SCOUT_QUESTIONS: Final = 8

# Per-question scout targets: web (default), a named repo path, a local-box probe.
SCOUT_TARGETS: Final = ("web", "repo", "local")
DEFAULT_SCOUT_TARGET: Final = "web"
SCOUT_MODES: Final = ("split", "combine")
DEFAULT_SCOUT_MODE: Final = "split"
# The one fan-out item of a combine run and of a bare ask.
COMBINED_SCOUT_ITEM: Final = "question"
# Opens each target line of a question's ask.md section (one line per target); a web-only
# question carries none.
TARGET_LINE_PREFIX: Final = "Target: "
# dispatch.emit params the CLI sends and the research route reads.
SCOUT_QUESTIONS_PARAM: Final = "scout_questions"
SCOUT_MODE_PARAM: Final = "scout_mode"

# Scouts-segment names: list of local-target question slugs, and the flag true iff it is non-empty.
PROBES_LIST: Final = "probes"
LOCAL_PROBE_FLAG: Final = "local_probe"
# Splits a target entry string: source, then (local only) probe id, then arg.
TARGET_ENTRY_SEP: Final = ":"
# A local arg is substituted into a shell command; only this charset (fullmatch) is admitted.
PROBE_ARG_PATTERN: Final = r"[A-Za-z0-9._-]+"
# DoE's probe allowlist, relative to the content root; a probe whose ref is PROBE_NO_REF takes no arg.
SCOUTS_ASK_FRAGMENT: Final = "contract/scouts-ask-fragment.json"
PROBE_ALLOWLIST_KEY: Final = "probe_allowlist"
PROBE_NO_REF: Final = "none"
# Keys of the sizing object's research block.
QUESTION_MODE_KEY: Final = "question_mode"
QUESTION_TARGETS_KEY: Final = "question_targets"


def seed_sources(seeds) -> list[str]:
    """Sources fed by `seeds` ({kind, url} mappings), first-seen order, no repeats; None or [] -> [].

    Raises ValueError on a kind outside SEED_KINDS.
    """
    out: list[str] = []
    for seed in seeds or ():
        kind = seed.get("kind")
        if kind not in SEED_KIND_SOURCE:
            raise ValueError(f"research.seeds kind {kind!r} is not one of {list(SEED_KINDS)}")
        if SEED_KIND_SOURCE[kind] not in out:
            out.append(SEED_KIND_SOURCE[kind])
    return out


class ScoutTarget(TypedDict):
    """One target (SCOUT_TARGETS) and its ref ("" for web)."""

    target: str
    ref: str


class ScoutQuestion(TypedDict):
    """One scout question: its text and its ordered, non-empty list of targets."""

    text: str
    targets: list[ScoutTarget]


class ShapeResult(TypedDict):
    """research.shape return: tier, the reason (names the notch and any floor), ordered manifests, per-pipeline flags."""

    tier: str
    reason: str
    pipelines: list[str]
    flags: dict[str, dict[str, str]]
