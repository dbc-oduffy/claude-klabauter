"""Research segments for the dispatch.emit research route: a shape result to ordered (pipeline, inputs).

`segments_for` turns a research.shape result into the ordered `(pipeline, PipelineInputs)` segments
the chain composer runs; `write_ask` writes the ask file the scouts read as `{{brief}}`;
`bind_context` names the caller's non-corpus context files in the brief every member reads;
`gate_notebooklm` binds the scope/scout halts and the strategy-driven worker fan-out onto the
notebooklm manifest, and `claims_output` names its sweep output for `research.close`'s `require_claims`.
Scout questions are slugged to [a-z0-9-] (at most 48 chars) because the scouts template names
`digest-{{item}}.md`.

`bind_repo_targets` fans a sizing's repo targets and repo seeds out to one keyed `repo` segment each (own scratch dir and
brief; a seed's brief carries the clone instruction) plus, from two refs, one `repo-compare` segment; `compare_output`
names that segment's deliverable. `bind_seed_briefs` gives each other seeded pipeline its own `seeds-<pipeline>` brief.

Negative-spec: loads and validates no manifest (`gate_notebooklm` only rewrites one it is handed), composes no script, resolves no content root; stats a
repo scout target and checks a local one against the probe allowlist, never running it; reads only the allowlist JSON
beside a content root it is handed; the only writes are ask.md and brief.md under the caller's repo-relative scratch dir,
the per-segment `<key>/brief.md` and `repo-compare/brief.md` files `bind_repo_targets` writes there and the
`seeds-<pipeline>/brief.md` files `bind_seed_briefs` writes; a repo seed is cloned by its segment's scope stage, never here.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import replace
from pathlib import Path
from typing import Mapping, Sequence

from coordinator_core.ops import _research_contract as rc
from coordinator_core.ops.dispatch_emit.pipeline_contract import (
    FAN_OUT_OVER, FanOut, HaltCount, Manifest, PipelineEmitRefused, PipelineInputs,
)

__all__ = ["bind_seed_briefs", "REPO_PIPELINE", "REPO_COMPARE_PIPELINE", "bind_repo_targets", "compare_output", "ASK_FILE", "BRIEF_FILE", "CORPUS_DEFAULT_LISTS", "LIST_KEY_SHAPES", "corpus_default_lists", "list_key_refusals", "MAX_SLUG_LEN", "multi_ask_preamble", "normalize_scout_questions", "probe_allowlist", "ask_digest", "run_id_for", "bind_context", "claims_output", "gate_notebooklm", "halt_legs", "scout_slugs", "segments_for", "write_ask"]

ASK_FILE = "ask.md"
BRIEF_FILE = "brief.md"
_CONTEXT_HEADING = "## Context files"
MAX_SLUG_LEN = 48
DIGEST_LEN = 6
SCRIPT_SUFFIX = ".workflow.mjs"
SUBMISSION_FILE = "submission.json"
_NON_SLUG_RE = re.compile(r"[^a-z0-9]+")
_TOPIC_FALLBACK = "question"
_ROSTER_LIST = "roster"
_QUESTIONS_LIST = "questions"
_NOTEBOOKS_LIST = "notebooks"
_NOTEBOOKLM_PIPELINE = "notebooklm"
REPO_PIPELINE = "repo"
REPO_COMPARE_PIPELINE = "repo-compare"

# The corpus scope templates fix these shapes: the repo scope defines exactly four chunks A-D
# (spanning every target, by the repos' architecture, not one per target) and assigns them to
# two Haiku scouts; the web scope defaults to four topics a-d. A caller's --list overrides.
CORPUS_DEFAULT_LISTS: dict[str, dict[str, tuple[str, ...]]] = {
    "repo": {"chunks": ("A", "B", "C", "D"), "haiku_scouts": ("1", "2")},
    "web": {"topics": ("a", "b", "c", "d")},
}


#: The keys each corpus scope template can key on. A list value outside these fans a member
#: out under a name the scope, mailboxes and rebuttals never use: its scout BLOCKs, or its
#: specialist's mailbox is never read (every rebuttal comes back `challenged: []`).
LIST_KEY_SHAPES: dict[str, dict[str, tuple[re.Pattern, str]]] = {
    "repo": {
        "chunks": (re.compile(r"^[A-D]$"), "the repo scope's chunk letters A-D"),
        "haiku_scouts": (re.compile(r"^[12]$"), "the repo scope's Haiku scout keys 1 and 2"),
    },
    "web": {"topics": (re.compile(r"^[a-z]$"), "the web scope's lowercase topic letters a, b, c, ...")},
}


def list_key_refusals(pipeline: str, lists: Mapping[str, Sequence[str]]) -> list[str]:
    """One refusal per list value the pipeline's scope template cannot key on."""
    out: list[str] = []
    for name, (pattern, meaning) in LIST_KEY_SHAPES.get(pipeline, {}).items():
        bad = [v for v in lists.get(name) or () if not pattern.match(str(v))]
        if bad:
            out.append(f"--list {name}={','.join(map(str, bad))}: values must be {meaning}")
    return out


def corpus_default_lists(pipeline: str, questions: Sequence[str] = ()) -> dict[str, tuple[str, ...]]:
    """`CORPUS_DEFAULT_LISTS[pipeline]`, with web topics seeded one letter per research question text."""
    lists = dict(CORPUS_DEFAULT_LISTS.get(pipeline, {}))
    if pipeline == "web" and questions:
        lists["topics"] = tuple("abcdefghijklmnopqrstuvwxyz"[: len(questions)])
    return lists


def ask_digest(root: Path, parts: Mapping[str, object]) -> str:
    """Six hex digits naming one research ask: the same `parts` under the same `root` digest alike."""
    blob = json.dumps({"root": root.resolve().as_posix(), **parts}, sort_keys=True, default=str)
    return hashlib.sha1(blob.encode("utf-8")).hexdigest()[:DIGEST_LEN]


def run_id_for(root: Path, run_dir_root: str, prefix: str, digest: str, now: str) -> str:
    """The run id for the ask `digest`: the newest unlaunched script already emitted for it, else a fresh id.

    A re-emit of one ask reuses its script's run id, so it rewrites that script instead of
    writing a sibling. A script whose run dir holds a `submission.json` has launched; reusing its
    id would collide with the run's token, submission and briefs, so a fresh id is minted.
    """
    base = root / run_dir_root
    found = sorted(base.glob(f"{prefix}*-{digest}{SCRIPT_SUFFIX}"), key=lambda p: p.stat().st_mtime, reverse=True)
    for script in found:
        run_id = script.name[: -len(SCRIPT_SUFFIX)]
        if not (base / run_id / SUBMISSION_FILE).exists():
            return run_id
    fresh, bump = f"{prefix}{now}-{digest}", 0
    while (base / fresh).exists() or (base / f"{fresh}{SCRIPT_SUFFIX}").exists():
        bump += 1
        fresh = f"{prefix}{now}{bump}-{digest}"
    return fresh


def _slug(text: str) -> str:
    """Slug of `text`, cut at the last `-` within MAX_SLUG_LEN; a single longer word hard-cuts."""
    slug = _NON_SLUG_RE.sub("-", text.lower()).strip("-")
    if len(slug) > MAX_SLUG_LEN:
        cut = slug.rfind("-", 0, MAX_SLUG_LEN + 1)
        slug = slug[:cut] if cut > 0 else slug[:MAX_SLUG_LEN]
    return slug.strip("-")


_GIVEN_SLUG_RE = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")


def given_slug_refusal(value: object) -> str:
    """The reason an EM-named topic slug is unusable, or "" when it is lowercase [a-z0-9] words joined by single `-` within MAX_SLUG_LEN (same shape the CLI enforces)."""
    if not isinstance(value, str) or not _GIVEN_SLUG_RE.fullmatch(value) or len(value) > MAX_SLUG_LEN:
        return f"topic_slug must be [a-z0-9-] and at most {MAX_SLUG_LEN} characters"
    return ""


def topic_slug(ask: str = "", sizing_rel: str = "") -> str:
    """The run's `research.close` topic: the sizing's stem without its date, else the ask's slug."""
    stem = re.sub(r"^\d{4}-\d{2}-\d{2}-", "", Path(sizing_rel).stem) if sizing_rel else ""
    return _slug(stem or ask) or _TOPIC_FALLBACK


_PROBE_ARG_RE = re.compile(rc.PROBE_ARG_PATTERN)


class _MalformedCliProbe(ValueError):
    """A CLI probe entry missing its tool, verb ref or a non-empty list of safe verbs."""


def _probe_takes(entry: Mapping) -> "bool | tuple[str, ...]":
    """One allowlist entry's value: a bool (takes an arg), or for a CLI probe its non-empty verb tuple; _MalformedCliProbe when a CLI entry is malformed."""
    if rc.PROBE_TOOL_KEY not in entry and rc.PROBE_VERBS_KEY not in entry:
        return entry.get("ref") != rc.PROBE_NO_REF
    tool, verbs = entry.get(rc.PROBE_TOOL_KEY), entry.get(rc.PROBE_VERBS_KEY)
    if not (isinstance(tool, str) and tool.strip() and entry.get("ref") == rc.PROBE_VERB_REF
            and isinstance(verbs, list) and verbs
            and all(isinstance(v, str) and _PROBE_ARG_RE.fullmatch(v) for v in verbs)):
        raise _MalformedCliProbe(
            f"CLI probe {entry.get('id')!r} needs a {rc.PROBE_TOOL_KEY}, ref {rc.PROBE_VERB_REF!r} and a non-empty {rc.PROBE_VERBS_KEY} list of {rc.PROBE_ARG_PATTERN} words"
        )
    return tuple(verbs)


def probe_allowlist(content_root: Path) -> dict[str, "bool | tuple[str, ...]"]:
    """DoE's probe allowlist as `{probe id: takes an arg, or a CLI probe's verb tuple}`, read from `<content_root>/contract/scouts-ask-fragment.json`.

    A probe whose `ref` is `PROBE_NO_REF` takes no arg. A CLI probe (an entry with `tool`/`verbs`)
    maps to its verb tuple instead of a bool. Raises one PipelineEmitRefused naming the
    file when it is missing, not JSON, or its `probe_allowlist` is absent or malformed, a malformed
    CLI entry included (an empty list is valid and refuses every local target). Runs no probe.
    """
    path = Path(content_root) / rc.SCOUTS_ASK_FRAGMENT
    try:
        entries = json.loads(path.read_text(encoding="utf-8"))[rc.PROBE_ALLOWLIST_KEY]
        return {e["id"]: _probe_takes(e) for e in entries}
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as err:
        detail = str(err) if isinstance(err, _MalformedCliProbe) else type(err).__name__
        raise PipelineEmitRefused([f"{path.as_posix()} has no readable {rc.PROBE_ALLOWLIST_KEY} ({detail})"]) from err


def _split_entry(entry: object) -> tuple[object, object]:
    """A target entry as (target, ref): a mapping `{target, ref}`, or the string `web | repo:<ref> | local:<probe>[:<arg>]`."""
    if isinstance(entry, Mapping):
        return entry.get("target") or rc.DEFAULT_SCOUT_TARGET, entry.get("ref") or ""
    if isinstance(entry, str):
        source, _, ref = entry.partition(rc.TARGET_ENTRY_SEP)
        return source, ref
    return None, None


def _local_ref_refusal(ref: str, allowlist: Mapping[str, "bool | tuple[str, ...]"] | None) -> str:
    if allowlist is None:
        return "local target cannot be checked: no probe allowlist is loaded"
    probe, sep, arg = ref.partition(rc.TARGET_ENTRY_SEP)
    if probe not in allowlist:
        return f"local probe {probe!r} is not in the probe allowlist ({', '.join(sorted(allowlist)) or 'empty'})"
    takes = allowlist[probe]
    if not takes:
        return f"local probe {probe!r} takes no arg" if sep else ""
    if not sep or not arg:
        return f"local probe {probe!r} needs an arg after '{rc.TARGET_ENTRY_SEP}'"
    if isinstance(takes, tuple):
        return "" if arg in takes else f"local probe {probe!r} verb {arg!r} is not allowed; allowed verbs: {', '.join(takes)}"
    if not _PROBE_ARG_RE.fullmatch(arg):
        return f"local probe arg {arg!r} must match {rc.PROBE_ARG_PATTERN}"
    return ""


def _normalize_target(
    root: Path, entry: object, allowlist: Mapping[str, bool] | None
) -> tuple[rc.ScoutTarget | None, str]:
    """One target entry as a `ScoutTarget` and "", or None and the refusal."""
    target, ref = _split_entry(entry)
    if not isinstance(target, str) or not isinstance(ref, str):
        return None, "target entry must be a string or a {target, ref} mapping"
    if target not in rc.SCOUT_TARGETS:
        return None, f"target {target!r} is not one of {', '.join(rc.SCOUT_TARGETS)}"
    if target == "web":
        return (None, "a web target takes no ref") if ref else ({"target": target, "ref": ""}, "")
    if target == "repo":
        path = Path(ref) if ref and Path(ref).is_absolute() else root / ref
        if not ref.strip() or not path.exists():
            return None, f"repo ref {ref!r} is not an existing file or directory"
        try:
            ref = path.resolve().relative_to(root.resolve()).as_posix()
        except ValueError:
            ref = path.resolve().as_posix()
        return {"target": target, "ref": ref}, ""
    reason = _local_ref_refusal(ref, allowlist)
    return (None, reason) if reason else ({"target": target, "ref": ref}, "")


def normalize_scout_questions(
    root: Path, questions: Sequence[str | Mapping], allowlist: Mapping[str, bool] | None = None
) -> list[rc.ScoutQuestion]:
    """Each question as a `ScoutQuestion`: a str is one web target; a mapping is validated.

    A mapping carries `text` and either the single `target`/`ref` or a `targets` list (not both),
    each entry `web | repo:<ref> | local:<probe>[:<arg>]` or `{target, ref}`. A repo ref must exist
    (absolute, else under `root`) and is stored repo-relative POSIX when under `root`, else absolute
    POSIX. A local ref is `<probe>[:<arg>]`, checked against `allowlist` (probe id -> takes an arg;
    any local target is refused when None) and never stat'd or run. A duplicate target within one
    question is refused. Raises one PipelineEmitRefused naming every bad "scout question <n>".
    """
    out: list[rc.ScoutQuestion] = []
    reasons: list[str] = []
    for index, raw in enumerate(questions):
        label = f"scout question {index + 1}"
        if isinstance(raw, str):
            out.append({"text": raw, "targets": [{"target": rc.DEFAULT_SCOUT_TARGET, "ref": ""}]})
            continue
        if not isinstance(raw, Mapping):
            reasons.append(f"{label} must be a string or a mapping")
            continue
        text = raw.get("text")
        if not isinstance(text, str):
            reasons.append(f"{label} has no text")
            continue
        if "targets" in raw and ("target" in raw or "ref" in raw):
            reasons.append(f"{label} gives both target/ref and targets; use one")
            continue
        entries = raw["targets"] if "targets" in raw else [{"target": raw.get("target"), "ref": raw.get("ref")}]
        if not isinstance(entries, (list, tuple)) or not entries:
            reasons.append(f"{label} targets must be a non-empty list")
            continue
        targets: list[rc.ScoutTarget] = []
        bad = False
        for entry in entries:
            normal, reason = _normalize_target(root, entry, allowlist)
            if normal is None:
                reasons.append(f"{label} {reason}")
                bad = True
            elif normal in targets:
                reasons.append(f"{label} repeats target {normal['target']}" + (f" {normal['ref']!r}" if normal["ref"] else ""))
                bad = True
            else:
                targets.append(normal)
        if not bad:
            out.append({"text": text, "targets": targets})
    if reasons:
        raise PipelineEmitRefused(reasons)
    return out


def multi_ask_preamble(questions: Sequence[rc.ScoutQuestion | str]) -> str:
    """`Research questions:` and a numbered list of every question's text."""
    texts = [q if isinstance(q, str) else q["text"] for q in questions]
    return "Research questions:\n\n" + "\n".join(f"{n}. {t.strip()}" for n, t in enumerate(texts, 1))


def _target_lines(question: rc.ScoutQuestion) -> list[str]:
    """One `Target:` line per target in list order; none for a question whose targets are exactly [web]."""
    targets = question["targets"]
    if all(t["target"] == rc.DEFAULT_SCOUT_TARGET for t in targets):
        return []
    return [
        f"{rc.TARGET_LINE_PREFIX}{t['target']}" + (f" `{t['ref']}`" if t["target"] != rc.DEFAULT_SCOUT_TARGET else "")
        for t in targets
    ]


def scout_slugs(questions: Sequence[str | rc.ScoutQuestion]) -> list[str]:
    """The [a-z0-9-] slug of each question, in order.

    Refuses more than MAX_SCOUT_QUESTIONS, a question with no slug-able character, and two
    questions that slug alike.
    """
    reasons: list[str] = []
    if len(questions) > rc.MAX_SCOUT_QUESTIONS:
        reasons.append(f"{len(questions)} scout questions given; at most {rc.MAX_SCOUT_QUESTIONS} are allowed")
    slugs: list[str] = []
    for index, question in enumerate(questions):
        text = question.get("text") if isinstance(question, Mapping) else question
        slug = _slug(text) if isinstance(text, str) else ""
        if not slug:
            reasons.append(f"scout question {index + 1} has no letter or digit to slug")
        elif slug in slugs:
            reasons.append(f"scout question {index + 1} slugs to {slug!r}, which an earlier question already took")
        slugs.append(slug)
    if reasons:
        raise PipelineEmitRefused(reasons)
    return slugs


def write_ask(
    root: Path,
    scratch_rel: str,
    ask: str,
    questions: Sequence[str | Mapping] = (),
    mode: str = rc.DEFAULT_SCOUT_MODE,
    allowlist: Mapping[str, bool] | None = None,
) -> str:
    """Write `<root>/<scratch_rel>/ask.md` and return its repo-relative POSIX path.

    No questions: the file is the ask verbatim. Otherwise the ask, then (two or more questions)
    `multi_ask_preamble`, then sections. Split: each question under a `## <slug>` heading, the
    section a scout finds by its `{{item}}`. Combine: one `## question` section, numbered. A question
    with a repo or local target has one `Target:` line per target before its text (combine: the first
    on the number line, the rest indented). Refuses an empty ask, an unknown mode, or questions
    `normalize_scout_questions` (given `allowlist`)/`scout_slugs` refuse.
    """
    if not isinstance(ask, str) or not ask.strip():
        raise PipelineEmitRefused(["the ask is empty"])
    if mode not in rc.SCOUT_MODES:
        raise PipelineEmitRefused([f"scout mode {mode!r} is not one of {', '.join(rc.SCOUT_MODES)}"])
    normalized = normalize_scout_questions(root, questions, allowlist)
    slugs = scout_slugs(normalized)
    text = ask
    if slugs:
        head = ask.rstrip("\n")
        if len(normalized) >= 2:
            head += "\n\n" + multi_ask_preamble(normalized)
        if mode == "combine":
            items = []
            for n, q in enumerate(normalized, 1):
                lines = _target_lines(q) + [q["text"].strip()]
                items.append(f"{n}. " + "\n   ".join(lines))
            text = head + f"\n\n## {rc.COMBINED_SCOUT_ITEM}\n\n" + "\n".join(items) + "\n"
        else:
            sections = []
            for slug, q in zip(slugs, normalized):
                lines = _target_lines(q)
                sections.append(f"\n\n## {slug}\n\n" + "".join(f"{line}\n" for line in lines) + ("\n" if lines else "") + q["text"].strip())
            text = head + "".join(sections) + "\n"
    rel = f"{scratch_rel.rstrip('/')}/{ASK_FILE}"
    target = root / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8", newline="\n")
    return rel


def bind_context(root: Path, scratch_rel: str, brief_rel: str, context: Sequence[str]) -> str:
    """Name `context` files in the brief; return the brief path members read.

    The ask file gains a `## Context files` section in place. Any other brief (a sizing) is never
    edited: `<scratch>/brief.md` points at it and carries the section, and that path is returned.
    Paths under `root` are written repo-relative, others absolute. Refuses a file that is missing.
    """
    if not context:
        return brief_rel
    names, missing = [], []
    for raw in context:
        path = Path(raw) if Path(raw).is_absolute() else root / raw
        if not path.is_file():
            # Members read the path later, so a pipe or `<(...)` is gone by then.
            missing.append(f"context file {raw} does not exist; --context takes a saved file path, not a pipe")
            continue
        try:
            names.append(path.resolve().relative_to(root.resolve()).as_posix())
        except ValueError:
            names.append(path.resolve().as_posix())
    if missing:
        raise PipelineEmitRefused(missing)
    section = (
        f"{_CONTEXT_HEADING}\n\nRead each before you start; they are sources outside every corpus.\n\n"
        + "".join(f"- `{name}`\n" for name in names)
    )
    ask_rel = f"{scratch_rel.rstrip('/')}/{ASK_FILE}"
    if brief_rel == ask_rel:
        target = root / ask_rel
        text = target.read_text(encoding="utf-8").rstrip("\n") + "\n\n" + section
        target.write_text(text, encoding="utf-8", newline="\n")
        return brief_rel
    rel = f"{scratch_rel.rstrip('/')}/{BRIEF_FILE}"
    target = root / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(f"The brief is `{brief_rel}`; read it first.\n\n{section}", encoding="utf-8", newline="\n")
    return rel


_GATED_STAGES = ("scope", "scout", "workers", "sweep")
_SCRATCH_PREFIX = "{{scratch_dir}}/"

_SCOPE_SCHEMA = {
    "type": "object",
    "properties": {
        "strategy_path": {"type": "string"},
        "worker_count": {"type": "integer"},
        "notebooks": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["strategy_path", "worker_count", "notebooks"],
}

_SCOUT_SCHEMA = {
    "type": "object",
    "properties": {
        "usable_sources": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "notebook": {"type": "string"},
                    "ref": {
                        "type": "string",
                        "description": (
                            "A source is usable only when its content was fetched. A 403 or other error "
                            "page, an empty page or a login wall is not usable. A notebook whose strategy "
                            "says research_start contributes one entry with ref research_start."
                        ),
                    },
                },
                "required": ["notebook", "ref"],
            },
        },
    },
    "required": ["usable_sources"],
}


def halt_legs(pipelines: Sequence[str]) -> dict[str, str]:
    """Leg map for the chained script: the preflight shares notebooklm's leg, so its halt skips notebooklm and nothing else."""
    if rc.PREFLIGHT_PIPELINE in pipelines and "notebooklm" in pipelines:
        return {rc.PREFLIGHT_PIPELINE: "notebooklm"}
    return {}


def gate_notebooklm(manifest: Manifest) -> Manifest:
    """The notebooklm manifest with the scope count gate, the scout usable-sources gate and workers fanned over strategy.md's notebooks.

    A stage that already declares a schema or halt keeps it. Refuses a manifest lacking any of the
    stage ids scope, scout, workers and sweep, naming the missing ones.
    """
    ids = {stage.id for stage in manifest.stages}
    missing = [sid for sid in _GATED_STAGES if sid not in ids]
    if missing:
        raise PipelineEmitRefused([
            f"notebooklm manifest lacks stage {sid!r}; the research gate binds scope, scout, workers and sweep"
            for sid in missing
        ])
    schemas = dict(manifest.schemas)
    stages = []
    for stage in manifest.stages:
        if stage.id == "scope":
            if stage.schema is None:
                schemas["<inline:scope>"] = _SCOPE_SCHEMA
                stage = replace(stage, schema="<inline:scope>")
            if stage.halts_unless_count is None:
                stage = replace(stage, halts_unless_count=HaltCount(("worker_count", "notebooks"), "notebooks"))
        elif stage.id == "scout":
            if stage.schema is None:
                schemas["<inline:scout>"] = _SCOUT_SCHEMA
                stage = replace(stage, schema="<inline:scout>")
            if stage.halts_unless_nonempty is None:
                stage = replace(stage, halts_unless_nonempty="usable_sources")
        elif stage.id == "workers":
            stage = replace(stage, fan_out=FanOut(kind=FAN_OUT_OVER, source="scope", field="notebooks"))
        stages.append(stage)
    return replace(manifest, stages=tuple(stages), schemas=schemas)


def claims_output(manifest: Manifest) -> str:
    """The sweep stage's output path relative to the segment's scratch dir."""
    sweep = next(stage for stage in manifest.stages if stage.id == "sweep")
    return sweep.output.removeprefix(_SCRATCH_PREFIX)


def compare_output(manifest: Manifest) -> str:
    """The repo-compare manifest's single stage output relative to its segment dir."""
    return manifest.stages[-1].output.removeprefix(_SCRATCH_PREFIX)


def _ref_slug(ref: str) -> str:
    parts = [p for p in re.split(r"[/\\]", ref.rstrip("/\\")) if p]
    return _slug(parts[-1]) if parts else ""


def _write_brief(root: Path, rel: str, text: str) -> None:
    target = root / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8", newline="\n")


def bind_seed_briefs(
    root: Path, scratch_rel: str, members_brief: str, seeds: Sequence[Mapping[str, str]],
) -> dict[str, str]:
    """`{pipeline: brief_rel}` for each non-repo pipeline a seed feeds; repo seeds are `bind_repo_targets`'.

    Writes `<scratch>/seeds-<pipeline>/brief.md`: it points at the members brief and its `## Seeds`
    section lists that pipeline's seed URLs and no others. Raises ValueError on an unknown seed kind.
    """
    by_source: dict[str, list[tuple[str, str]]] = {}
    for seed in seeds:
        kind = seed.get("kind")
        source = rc.SEED_KIND_SOURCE.get(kind)
        if source is None:
            raise ValueError(f"research.seeds kind {kind!r} is not one of {list(rc.SEED_KINDS)}")
        if source != REPO_PIPELINE:
            by_source.setdefault(source, []).append((kind, seed["url"]))
    out: dict[str, str] = {}
    base = scratch_rel.rstrip("/")
    for source, rows in by_source.items():
        rel = f"{base}/seeds-{source}/{BRIEF_FILE}"
        _write_brief(root, rel, (
            f"The brief is `{members_brief}`; read it first.\n\n"
            "## Seeds\n\nThe PM named these sources to study. Start from them.\n\n"
            + "".join(f"- {kind}: {url}\n" for kind, url in rows)
        ))
        out[source] = rel
    return out


def bind_repo_targets(
    root: Path, scratch_rel: str, members_brief: str, targets: Sequence[Mapping[str, str]],
    seeds: Sequence[Mapping[str, str]] = (),
) -> tuple[list[tuple[str, str]], str]:
    """Per-ref repo segment (key, brief_rel) pairs and the compare brief path.

    Refs are the repo targets, then the repo seeds. Without seeds, under two repo targets gives
    ([], ""). With a repo seed every ref gets a segment; the compare brief is "" under two refs.

    Key i (1-based, ref order) is `repo-<i>-<slug of the ref's last path component>`, cut to
    MAX_SLUG_LEN. Writes `<scratch>/<key>/brief.md` per ref and `<scratch>/repo-compare/brief.md`.
    A seed's brief names the URL and the clone dir `<scratch>/<key>/clone`; the segment's scope
    stage runs the clone, this function spawns nothing.
    Refuses a ref with no slug-able character (PipelineEmitRefused naming the ref index).
    """
    refs = [t.get("ref", "") for t in targets if t.get("source") == REPO_PIPELINE]
    seed_urls = [s["url"] for s in seeds if rc.SEED_KIND_SOURCE.get(s.get("kind")) == REPO_PIPELINE]
    if len(refs) < 2 and not seed_urls:
        return [], ""
    seed_from = len(refs)
    refs = refs + seed_urls
    base = scratch_rel.rstrip("/")
    keys: list[tuple[str, str]] = []
    reasons: list[str] = []
    for index, ref in enumerate(refs, start=1):
        slug = _ref_slug(ref)
        if not slug:
            reasons.append(f"repo {'seed' if index > seed_from else 'target'} {index} ({ref!r}) has no letter or digit to slug")
            continue
        prefix = f"{REPO_PIPELINE}-{index}-"
        key = (prefix + slug)[:MAX_SLUG_LEN].rstrip("-")
        keys.append((key, ref))
    if reasons:
        raise PipelineEmitRefused(reasons)
    segments: list[tuple[str, str]] = []
    for index, (key, ref) in enumerate(keys):
        rel = f"{base}/{key}/{BRIEF_FILE}"
        if index < seed_from:
            body = (
                f"This segment studies the repo research target `{ref}` alone. It is the primary repo, "
                "and no other repo target is folded in.\n\n"
            )
        else:
            clone = f"{base}/{key}/clone"
            body = (
                f"This segment studies the repo seed `{ref}` alone. It is a remote repo: when `{clone}` "
                f"is absent, shallow-clone it there (`git clone --depth 1 -- {ref} {clone}`), then treat "
                "the clone as the primary repo. No other repo is folded in.\n\n"
            )
        _write_brief(root, rel, (
            f"The brief is `{members_brief}`; read it first.\n\n" + body
            + "A requested output path in the brief does not apply here; write only under this segment's scratch dir.\n"
        ))
        segments.append((key, rel))
    if len(keys) < 2:
        return segments, ""
    rows = "".join(
        f"- `{key}`: `{ref}`\n  - `{base}/{key}/synthesis.md`\n"
        f"  - `{base}/{key}/synthesis-pass-2.md` (read when present, for a deepest run)\n"
        for key, ref in keys
    )
    compare_rel = f"{base}/{REPO_COMPARE_PIPELINE}/{BRIEF_FILE}"
    _write_brief(root, compare_rel, (
        f"The brief is `{members_brief}`; read it first.\n\n"
        f"## Repo segments\n\n{rows}\n"
        "Compare the repos against the ask, rank them with the reason for each rank, "
        "and cite each claim to the segment synthesis it came from. "
        "A segment with no synthesis halted: name it as unranked rather than omit it.\n"
    ))
    return segments, compare_rel


def _deep_roster(sources: Sequence[str]) -> list[dict[str, str]]:
    roster = [{"slug": role, "agent_type": rc.UNBLOCK_ROLE_AGENT_TYPE} for role in rc.UNBLOCK_ROLES]
    for source in sources or ("web",):
        agent_type = rc.SOURCE_SPECIALIST.get(source)
        if agent_type and not any(m["slug"] == source for m in roster):
            roster.append({"slug": source, "agent_type": agent_type})
    return roster


def _has_local_target(question: str | Mapping) -> bool:
    """Whether a raw or normalised question names a local target (any input form)."""
    if not isinstance(question, Mapping):
        return False
    entries = question["targets"] if "targets" in question else [question]
    return any(_split_entry(e)[0] == "local" for e in entries if isinstance(e, (str, Mapping)))


def segments_for(
    shape: rc.ShapeResult,
    *,
    brief_rel: str,
    scratch_rel: str,
    questions: Sequence[str | Mapping] = (),
    sources: Sequence[str] = (),
    targets: Sequence[Mapping[str, str]] = (),
    mode: str = rc.DEFAULT_SCOUT_MODE,
    repo_segments: Sequence[tuple[str, str]] = (),
    compare_brief: str = "",
    seed_briefs: Mapping[str, str] | None = None,
) -> list[tuple[str, PipelineInputs]]:
    """One `(pipeline, PipelineInputs)` per manifest in `shape["pipelines"]`, in order.

    Every segment binds `brief_rel` and `scratch_rel` and the flags `shape["flags"]` names for its
    pipeline. The scouts segment carries the slugs of `questions` (with none, or in combine `mode`,
    the one entry `question`, the bare or combined ask), the list `probes` (slugs of the questions
    with a local target; combine: `question` when any has one) and the flag `local_probe`
    (true iff `probes` is non-empty); the unblock segment carries the deep roster, three role members plus
    one specialist per `sources` entry that has one; the notebooklm segment carries the refs of
    the `targets` whose source is notebooklm as its notebooks. `CORPUS_DEFAULT_LISTS` is bound by
    the caller, which sees which lists each loaded manifest declares. With `repo_segments`, the
    `repo` pipeline expands to one keyed segment per `(key, brief_rel)` entry, followed by one
    `repo-compare` segment bound to `compare_brief`, omitted when that is "". A pipeline named in
    `seed_briefs` binds that brief in place of `brief_rel`.
    """
    seed_briefs = seed_briefs or {}
    shaped_flags = shape.get("flags") or {}
    segments: list[tuple[str, PipelineInputs]] = []
    for pipeline in shape["pipelines"]:
        flags: dict[str, str | bool] = dict(shaped_flags.get(pipeline) or {})
        lists: dict[str, tuple] = {}
        if pipeline == rc.SCOUTS_PIPELINE:
            if mode not in rc.SCOUT_MODES:
                raise PipelineEmitRefused([f"scout mode {mode!r} is not one of {', '.join(rc.SCOUT_MODES)}"])
            slugs = scout_slugs(questions) if questions else []
            local = [slug for slug, q in zip(slugs, questions) if _has_local_target(q)]
            if mode == "combine" or not slugs:
                slugs = [rc.COMBINED_SCOUT_ITEM]
            lists[_QUESTIONS_LIST] = tuple(slugs)
            # Bound only when a question has a local target: a manifest that predates the probe
            # stage (DoE's live one, until it ships) refuses any undeclared list or flag.
            if local:
                lists[rc.PROBES_LIST] = (rc.COMBINED_SCOUT_ITEM,) if mode == "combine" else tuple(local)
                flags[rc.LOCAL_PROBE_FLAG] = True
        elif pipeline == rc.DEEP_PIPELINE:
            lists[_ROSTER_LIST] = tuple(_deep_roster(sources))
        elif pipeline == _NOTEBOOKLM_PIPELINE:
            refs = tuple(t["ref"] for t in targets if t.get("source") == _NOTEBOOKLM_PIPELINE)
            if refs:
                lists[_NOTEBOOKS_LIST] = refs
        if pipeline == REPO_PIPELINE and repo_segments:
            for key, key_brief in repo_segments:
                segments.append((pipeline, PipelineInputs(
                    brief=key_brief, subjects=(), scratch_dir=scratch_rel, flags=dict(flags), lists=dict(lists), segment_key=key,
                )))
            if compare_brief:
                segments.append((REPO_COMPARE_PIPELINE, PipelineInputs(
                    brief=compare_brief, subjects=(), scratch_dir=scratch_rel,
                    flags=dict(shaped_flags.get(REPO_COMPARE_PIPELINE) or {}), lists={},
                )))
            continue
        segments.append(
            (pipeline, PipelineInputs(brief=seed_briefs.get(pipeline, brief_rel), subjects=(), scratch_dir=scratch_rel, flags=flags, lists=lists))
        )
    return segments
