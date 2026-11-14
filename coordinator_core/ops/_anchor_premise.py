"""Anchor premises: parse the plan's declared load-bearing premises and compute the findings
that keep a plan with an unchecked one from reading as ready. No IO, no spawn.

Shape: frontmatter ``anchor_premises:`` is a list of ``{id, claim, check: {command, expect}}``
(``[]`` when the plan rests on none). A premise-checker verdict row names its anchor with
``anchor: <id>`` or a citation opening ``anchor <id>``, where the spec may be a list or range of ids. A RESOLVES row naming no anchor otherwise clears
an anchor whose id is a whole token of its citation or evidence.
``line`` is the 1-based plan line of that anchor's ``command:``.

Negative-spec: the engine never spawns or walks for a check and never reads prose for
premises. It screens a command (``census.screen``), evaluates the ``_anchor_check`` subset
in-process, and fills only the no-verdict gap; a verdict is only matched by stripped id, and a
verdict outside ``VERDICTS`` counts as none.
"""

import re
from pathlib import Path
from typing import Any, Optional

from coordinator_core.ops import _anchor_check, anchor_lane
from coordinator_core.ops._research_grounding import line_hashes

FIELD = "anchor_premises"
VERDICTS: frozenset = frozenset({"RESOLVES", "UNRESOLVED", "CONTRADICTED", "UNCHECKABLE"})
_CLAIM_CAP = 160


def _text(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _unchecked(label: str, reason: str) -> str:
    return f"anchor-premise-unchecked: {label} ({reason})"


def parse_anchors(
    fm: dict, repo_root: Optional[Path], spine_rows: Optional[list] = None,
    base_ref: Optional[str] = None,
) -> tuple[list[dict], list[str]]:
    """(well-formed anchors, defect findings) for the plan frontmatter ``fm``. When the plan's
    ``spine_rows`` are passed, an anchor naming no ref whose checked file a row writes is refused,
    unless ``base_ref`` is set: the check then reads that ref, not the tree the rows write."""
    writes = _spine_writes(spine_rows) if base_ref is None else set()
    if FIELD not in fm:
        return [], [
            "anchor-premises-undeclared (declare each load-bearing premise as "
            "{id, claim, check: {command, expect}}, or [] when the plan rests on none)"
        ]
    entries = fm.get(FIELD)
    if not isinstance(entries, list):
        return [], ["anchor-premises-malformed"]

    from coordinator_core.roadmap import census

    tracked: Optional[frozenset] = None
    if repo_root is not None:
        tracked = census.tracked_targets(
            repo_root,
            [
                c
                for e in entries
                if isinstance(e, dict) and isinstance(e.get("check"), dict)
                for c in census.python_candidates(str(e["check"].get("command") or ""), repo_root)
            ],
        )

    anchors: list[dict] = []
    findings: list[str] = []
    seen: set[str] = set()
    for index, entry in enumerate(entries):
        label = f"#{index}"
        if not isinstance(entry, dict):
            findings.append(_unchecked(label, "not a mapping"))
            continue
        if _text(entry.get("id")):
            label = entry["id"].strip()
        if not _text(entry.get("id")) or not _text(entry.get("claim")):
            findings.append(_unchecked(label, "id and claim must be non-blank strings"))
            continue
        if label in seen:
            findings.append(_unchecked(label, "duplicate id"))
            continue
        seen.add(label)
        check = entry.get("check")
        if not isinstance(check, dict):
            findings.append(_unchecked(label, "check must be {command, expect}"))
            continue
        if not _text(check.get("command")) or not _text(check.get("expect")):
            findings.append(_unchecked(label, "check needs a non-blank command and expect"))
            continue
        refusal = census.screen(check["command"], repo_root, tracked)
        if refusal:
            findings.append(
                _unchecked(label, f"{refusal}; allowed form: {census.ALLOWED_FORM}")
            )
            continue
        written = _self_falsified(entry, writes, fm)
        if written:
            findings.append(
                _unchecked(
                    label,
                    f"self-falsified: {written} is written by a plan row, so the plan's own run "
                    "breaks this premise; name `baseline_ref <sha>` in the claim",
                )
            )
            continue
        anchors.append({**entry, "id": label})
    return anchors, findings


_SCREEN_REFUSED = "anchor-premise-unchecked: "


def split_screen_refused(findings: list) -> tuple[list, list[tuple[str, str]]]:
    """(findings that still block, (id, reason) per unevaluable check). A check the census screen
    refuses, or that lacks a usable command/expect, is unsatisfiable by design in every lane; an
    entry without a usable id and claim, or a duplicate id, stays a blocker."""
    kept: list = []
    refused: list[tuple[str, str]] = []
    for finding in findings:
        if isinstance(finding, str) and finding.startswith(_SCREEN_REFUSED):
            label, _, rest = finding[len(_SCREEN_REFUSED):].partition(" (")
            reason = rest.partition("; allowed form: ")[0].rstrip(")")
            if "; allowed form: " in finding or reason.startswith("check "):
                refused.append((label, reason))
                continue
        kept.append(finding)
    return kept, refused


_UNCHECKABLE_FINDING = re.compile(r"anchor-premise-UNCHECKABLE: (\S+) -- (.*)", re.DOTALL)


def split_convertible(findings: list) -> tuple[list, list[tuple[str, str]]]:
    """(findings that still block, (anchor id, claim) per UNCHECKABLE anchor). An UNCHECKABLE anchor
    is a premise no check could settle at prep time; it converts to an execution-time falsifier row
    instead of blocking readiness."""
    kept: list = []
    converted: list[tuple[str, str]] = []
    for finding in findings:
        match = _UNCHECKABLE_FINDING.fullmatch(finding) if isinstance(finding, str) else None
        if match:
            converted.append((match.group(1), match.group(2)))
        else:
            kept.append(finding)
    return kept, converted


def unjudged(reason: str) -> str:
    return f"anchor-premises-unjudged: {reason}"


_ID = r"[A-Za-z0-9_][A-Za-z0-9_.-]*"
# The premise checker writes "anchor premise <id>: ..."; the filler word is never an id.
_CITED = re.compile(rf"^\s*anchors?[:\s]+(?:premises?[:\s]+)?({_ID}(?:,\s*{_ID})*)", re.IGNORECASE)
_RANGE = re.compile(r"^(\D.*?)(\d+)(?:-|\.\.)(\1)(\d+)$")
_RANGE_CAP = 50


def _named(row: Any) -> Optional[str]:
    """The anchor id spec a row names: its ``anchor`` key, else a citation opening ``anchor <spec>``."""
    if not isinstance(row, dict):
        return None
    named = row.get("anchor")
    if isinstance(named, str):
        return named.strip()
    cited = row.get("citation")
    match = _CITED.match(cited) if isinstance(cited, str) else None
    return match.group(1).rstrip(".-") if match else None


def _expand(spec: str, known: frozenset) -> list[str]:
    """Ids a spec names: the whole spec when declared, else its list/range members (a range of
    at most ``_RANGE_CAP`` ids), else the spec itself."""
    if spec in known:
        return [spec]
    out: list[str] = []
    for token in (t.rstrip(".-") for t in re.split(r"[,\s]+", spec) if t):
        span = _RANGE.match(token)
        if token not in known and span and 0 <= int(span[4]) - int(span[2]) < _RANGE_CAP:
            width = len(span[2]) if span[2].startswith("0") else 0
            out += [f"{span[1]}{n:0{width}d}" for n in range(int(span[2]), int(span[4]) + 1)]
        else:
            out.append(token)
    return out or [spec]


def named_ids(row: Any, known: frozenset) -> Optional[list[str]]:
    """Every id a row's anchor spec names, or None when it names none."""
    spec = _named(row)
    return None if spec is None else _expand(spec, known)


def _valid(verdict: Any) -> bool:
    return isinstance(verdict, str) and verdict in VERDICTS


def _mentions(row: Any, anchor_id: str) -> bool:
    """True when a RESOLVES row carries ``anchor_id`` as a whole token in its citation or evidence,
    or an UNCHECKABLE row's citation opens with it (an incidental mention never downgrades a check)."""
    if not isinstance(row, dict) or row.get("verdict") not in ("RESOLVES", "UNCHECKABLE"):
        return False
    pattern = re.compile(r"(?<![A-Za-z0-9_.-])" + re.escape(anchor_id) + r"(?![A-Za-z0-9_-])")
    if row["verdict"] == "UNCHECKABLE":
        cited = row.get("citation")
        return isinstance(cited, str) and bool(re.match(r"\s*" + pattern.pattern, cited))
    return any(
        isinstance(row.get(key), str) and pattern.search(row[key]) for key in ("citation", "evidence")
    )


def _names(row: Any, anchor_id: str, known: frozenset = frozenset()) -> bool:
    ids = named_ids(row, known | {anchor_id})
    return anchor_id in ids if ids is not None else _mentions(row, anchor_id)


def _verdict_for(
    anchor_id: str, passed: list, carried: list, live: frozenset, known: frozenset = frozenset()
) -> tuple[Optional[str], str]:
    """(verdict, source) from the passed rows, then the live carry rows; ``(None, "none")`` else."""
    for row in passed:
        if _names(row, anchor_id, known) and _valid(row.get("verdict")):
            return row["verdict"], "row"
    for row in carried:
        if (
            _names(row, anchor_id, known)
            and _valid(row.get("verdict"))
            and row.get("line_hash") is not None
            and row.get("line_hash") in live
        ):
            return row["verdict"], "carry"
    return None, "none"


_ENGINE_VERDICT = {"held": "RESOLVES", "broke": "CONTRADICTED"}


REANCHORED = "re-anchored at baseline_ref"


def _own_context(plan_text: str, repo_root: Path) -> Optional[tuple[str, dict]]:
    """(baseline_ref, {path: run_id} the plan's own run wrote) or None when the plan names no
    sizing, the run wrote nothing, or no baseline resolves. The baseline is the plan's falsifier
    ``baseline_ref``, else the first halt of the run's halt chain."""
    from coordinator_core.ops import _own_run_premise
    from coordinator_core.roadmap.prep_gate import plan_frontmatter

    try:
        fm = plan_frontmatter(plan_text)
        ref = _falsifier_ref(fm)
        own = _own_run_premise.own_run_paths(Path(repo_root), fm.get("sizing_object"))
        if own and not ref:
            ref = _run_baseline(Path(repo_root), next(iter(own.values())))
    except Exception:
        return None
    return (ref, own) if own and ref else None


def _run_baseline(repo_root: Path, run_id: str) -> Optional[str]:
    """The sha ``run_id`` began from: the first halt of its ledger-check halt chain, else the
    ``run_base_sha`` its halt record names (a review_blocked halt joins no chain). The record is
    the worktree ``halted.json``, else the one in the run's halt commit (a committed halt leaves
    no worktree record). Up to three ``git log`` spawns; only reached on the broke path."""
    from coordinator_core.git.run import run_git
    from coordinator_core.ops.dispatch_emit import ask_contract as ac
    from coordinator_core.ops.dispatch_emit import halt_baseline
    from coordinator_core.ops.dispatch_emit.emit import resume_landed_state

    rel = f"{ac.RECORD_DIR_ROOT}/{run_id}/halted.json"
    halted = ac._read_json_object(ac.record_dir(repo_root, run_id) / "halted.json")
    plan_rel = (halted or {}).get(ac.HALT_PLAN_FIELD)
    if halted is None or not _text(plan_rel):
        done = run_git(
            ["log", "-n", "1", f"--grep=^warp: record halted {run_id} ",
             f"--format=%H%x1f%(trailers:key={ac.HALT_PLAN_TRAILER},valueonly)"],
            cwd=str(repo_root),
        )
        sha, _, trailer = done.stdout.partition("\x1f") if done.returncode == 0 else ("", "", "")
        sha = sha.strip()
        if halted is None and sha:
            halted = ac._read_json_at_commit(repo_root, sha, rel)
        if not _text(plan_rel):
            plan_rel = trailer.strip()
    if _text(plan_rel):
        state = resume_landed_state(repo_root, plan_rel)
        first = halt_baseline.chain_first_halt(repo_root, plan_rel, run_id, state)
        if first:
            return first
    base = (halted or {}).get("run_base_sha")
    return base if isinstance(base, str) and ac._FULL_SHA_RE.fullmatch(base) else None


def _checked_paths(anchor: dict) -> Optional[list[str]]:
    """The files an anchor's check reads, or None when the in-process evaluator cannot parse it."""
    from coordinator_core.ops import _anchor_check

    command = str((anchor.get("check") or {}).get("command") or "")
    try:
        return sorted({stage[2] for stage in _anchor_check._parse_stages(command)})
    except _anchor_check._NotEvaluable:
        return None


def _evaluate_at_ref(anchor: dict, ref: str, paths: list[str], repo_root: Path):
    """``anchor_lane.evaluate`` over the checked ``paths`` as they stand at ``ref`` (one git show
    per file, into a tempdir); None when a path cannot be read at the ref."""
    from coordinator_core.git.run import run_git
    from coordinator_core.temp_layout import managed_temp_dir

    with managed_temp_dir("anchor-baseline-", repo_root) as tmp:
        for rel in paths:
            done = run_git(["-C", str(repo_root), "show", f"{ref}:{rel}"], binary=True)
            if done.returncode != 0:
                return None
            target = Path(tmp) / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(done.stdout_bytes)
        return anchor_lane.evaluate(anchor, tmp)


def _reanchor(anchor: dict, plan_text: str, repo_root: Path) -> Optional[str]:
    """The observed text when a working-tree-broken anchor holds at the plan's baseline_ref and
    every file it checks was written by the plan's own run (post-conversion drift); else None.
    One git read per checked file, only on the broke path."""
    from coordinator_core.ops.dispatch_emit import halted_withheld_index as index

    ctx = _own_context(plan_text, repo_root)
    if ctx is None:
        return None
    ref, own = ctx
    paths = _checked_paths(anchor)
    if not paths or any(index._norm(p) not in own for p in paths):
        return None
    result = _evaluate_at_ref(anchor, ref, paths, repo_root)
    if result is None or result.outcome != "held":
        return None
    return f"{REANCHORED} {ref[:12]}: {result.observed or ''}".rstrip()


_CLAIM_REF = re.compile(r"\bbaseline_ref\b[\s:=`'\"]*([0-9a-fA-F]{7,40})\b")
_BARE_CLAIM_REF = re.compile(r"\bbaseline_ref\b")


def _falsifier_ref(fm: dict) -> Optional[str]:
    pec = fm.get("prime_exit_criterion")
    falsifier = pec.get("falsifier") if isinstance(pec, dict) else None
    ref = falsifier.get("baseline_ref") if isinstance(falsifier, dict) else None
    return ref.strip() if _text(ref) else None


def claimed_ref(claim: Any, fm: dict) -> Optional[str]:
    """The ref an anchor claim describes: the sha after ``baseline_ref`` in the claim, else the
    plan's falsifier ``baseline_ref`` when the claim says ``baseline_ref`` without one, else None."""
    if not isinstance(claim, str):
        return None
    match = _CLAIM_REF.search(claim)
    if match:
        return match.group(1)
    return _falsifier_ref(fm) if _BARE_CLAIM_REF.search(claim) else None


def _plan_fm(plan_text: str) -> dict:
    from coordinator_core.roadmap.prep_gate import plan_frontmatter

    try:
        return plan_frontmatter(plan_text)
    except Exception:
        return {}


def _evaluate(
    anchor: dict, fm: dict, repo_root: Path, base_ref: Optional[str] = None, blobs=None
):
    """The engine check for ``anchor``. Precedence: a ``git grep ... <ref> -- path`` stage's own
    ref, then the ref its claim names (``baseline_ref <sha>``) when readable, then ``base_ref``
    (read from ``blobs``, never a spawn), else the working tree. An unreadable claim ref (typo,
    unfetched sha) falls back to the tree silently; an unreadable ``base_ref`` is NOT_EVALUABLE."""
    ref = claimed_ref(anchor.get("claim"), fm)
    paths = _checked_paths(anchor) if ref else None
    if ref and paths:
        result = _evaluate_at_ref(anchor, ref, paths, repo_root)
        if result is not None:
            return result
    governing = None if ref else base_ref
    if governing is None and not blobs:
        return anchor_lane.evaluate(anchor, repo_root)
    return anchor_lane.evaluate(anchor, repo_root, governing, blobs)


_EXACT = re.compile(r"\d+")
_COUNT_CLAIM = re.compile(
    r"\b(\d+|one|two|three|four|five|six|seven|eight|nine|ten|once|twice|only|exactly|single|"
    r"sole|unique|count|times)\b",
    re.IGNORECASE,
)


def _overmatched(anchor: dict, result: Any) -> bool:
    """True when a broke check only OVER-matched an existence claim: every stage's expect is an exact
    count of at least one and its observed count is at least that, with some stage above, and the
    claim states no count. The checked text is present (what an existence claim asserts), so the
    count says the pattern is loose, not that the claim is false; a shortfall, an exact 0, a bound
    expect (>=, <=, <, >) or a claim that itself counts ("one refuse") is never an over-match."""
    if _COUNT_CLAIM.search(str(anchor.get("claim") or "")):
        return False
    counts = str(result.observed or "").split()
    wants = [w.strip() for w in str((anchor.get("check") or {}).get("expect") or "").split(" then ")]
    if len(wants) == 1:
        wants = wants * len(counts)
    if not counts or len(counts) != len(wants):
        return False
    pairs = []
    for want, got in zip(wants, counts):
        if not (_EXACT.fullmatch(want) and _EXACT.fullmatch(got)) or int(want) < 1:
            return False
        pairs.append((int(want), int(got)))
    return all(got >= want for want, got in pairs) and any(got > want for want, got in pairs)


def _spine_writes(spine_rows: Any) -> set[str]:
    """Normalised paths any spine row declares in ``writes``."""
    from coordinator_core.ops.dispatch_emit import halted_withheld_index as index

    out: set[str] = set()
    for row in spine_rows if isinstance(spine_rows, list) else []:
        writes = row.get("writes") if isinstance(row, dict) else None
        if isinstance(writes, list):
            out.update(index._norm(w) for w in writes if isinstance(w, str))
    return out


def _self_falsified(anchor: dict, writes: set[str], fm: dict) -> Optional[str]:
    """The checked path a spine row writes when ``anchor`` names no ref, else None."""
    from coordinator_core.ops.dispatch_emit import halted_withheld_index as index

    if not writes or claimed_ref(anchor.get("claim"), fm):
        return None
    for path in _checked_paths(anchor) or []:
        if index._norm(path) in writes:
            return path
    return None


def _blobs_for(anchors: list, fm: dict, repo_root: Path, base_ref: Optional[str]) -> dict:
    """Every (ref, path) blob the engine checks of ``anchors`` read, from ONE cat-file spawn; no
    spawn when no check reads a ref. An anchor whose claim names a ref reads that ref, not
    ``base_ref``, so only its own git-grep stage refs are collected."""
    pairs: list[tuple[str, str]] = []
    for anchor in anchors:
        command = str((anchor.get("check") or {}).get("command") or "")
        governing = None if claimed_ref(anchor.get("claim"), fm) else base_ref
        pairs += _anchor_check.blob_pairs(command, governing)
    return _anchor_check.read_at_ref(repo_root, pairs) if pairs else {}


def anchor_verdicts(
    anchors, rows, carry_rows, plan_text, repo_root, base_ref: Optional[str] = None
) -> list[dict]:
    """One {id, verdict, source, observed} per declared anchor, in declaration order.
    ``base_ref`` (the run's base ref) is read by every engine check whose claim names no
    ``baseline_ref``: precedence is a git-grep stage's own ref > claim ``baseline_ref`` > ``base_ref``
    > working tree. All refs are read in one git spawn; an unreadable ``base_ref`` leaves source
    "none" with ``observed`` naming it. Under ``base_ref`` the tree-drift remedy ``_reanchor`` is
    skipped. No ``base_ref`` and no git-grep stage: tree read, zero spawns.
    source: "row" | "carry" | "engine-check" | "none"; verdict is None only when source is "none".
    Precedence: passed row > live carry row > engine check (MATCH -> RESOLVES,
    MISMATCH -> CONTRADICTED) > none. A row or carry verdict other than RESOLVES yields to a
    met engine check (the declared check holds, so the premise is confirmed). NOT_EVALUABLE leaves source "none". A MISMATCH on a file the
    plan's own run wrote that held at the plan's baseline_ref is RESOLVES with ``observed``
    opening ``REANCHORED`` (source stays "engine-check": the reply schema enumerates sources). A row or
    carry CONTRADICTED on such a file takes the same regrade and keeps its source."""
    live = frozenset(line_hashes(plan_text))
    known = frozenset(a["id"] for a in anchors)
    fm = _plan_fm(plan_text) if repo_root is not None else {}
    out: list[dict] = []
    cache: list = []

    def evaluate(anchor):
        if not cache:
            cache.append(_blobs_for(anchors, fm, repo_root, base_ref))
        return _evaluate(anchor, fm, repo_root, base_ref, cache[0])

    for anchor in anchors:
        verdict, source = _verdict_for(anchor["id"], rows or [], carry_rows or [], live, known)
        observed = ""
        if verdict not in (None, "RESOLVES") and repo_root is not None:
            result = evaluate(anchor)
            if result.outcome == "held":
                verdict, source, observed = "RESOLVES", "engine-check", result.observed or ""
            elif verdict == "CONTRADICTED" and not (base_ref and not claimed_ref(anchor.get("claim"), fm)):  # same gate as the engine branch below
                moved = _reanchor(anchor, plan_text, repo_root)
                if moved is not None:
                    verdict, observed = "RESOLVES", moved
        if verdict is None and repo_root is not None:
            result = evaluate(anchor)
            observed = result.observed or ""
            engine = _ENGINE_VERDICT.get(result.outcome)
            if engine == "CONTRADICTED" and _overmatched(anchor, result):
                engine = None
                observed = (
                    f"check over-matched (observed {' then '.join(observed.split())}, expected "
                    f"{result.expect}): the pattern is loose; tighten it"
                )
            if engine is not None:
                verdict, source = engine, "engine-check"
            if engine == "CONTRADICTED" and not (
                base_ref and not claimed_ref(anchor.get("claim"), fm)
            ):
                moved = _reanchor(anchor, plan_text, repo_root)
                if moved is not None:
                    verdict, observed = "RESOLVES", moved
        out.append({"id": anchor["id"], "verdict": verdict, "source": source, "observed": observed})
    return out


def unevaluable_anchors(verdicts: list) -> list[tuple[str, str]]:
    """(id, detail) per none-source anchor whose engine check was NOT_EVALUABLE."""
    return [(v["id"], v["observed"]) for v in verdicts if v["verdict"] is None and v["observed"]]


def anchor_findings(
    anchors: list,
    rows: list,
    carry_rows: list,
    plan_text: str,
    repo_root: Optional[Path] = None,
    verdicts: Optional[list] = None,
    base_ref: Optional[str] = None,
) -> list[str]:
    """One finding per anchor not resolved. ``base_ref`` is forwarded to ``anchor_verdicts`` (same
    precedence: stage ref > claim ``baseline_ref`` > ``base_ref`` > tree) when ``verdicts`` is None. Passed ``rows`` win over ``carry_rows``; a carry row
    counts only while its ``line_hash`` is still a line of ``plan_text``. A none-source anchor
    whose check was NOT_EVALUABLE emits no finding in either lane: a check the in-process
    evaluator cannot run is never a blocker, and ``unevaluable_anchors`` carries it for the
    readiness consumer. A none-source anchor never evaluated (no repo root) still gets the
    bare no-verdict finding."""
    out: list[str] = []
    known = frozenset(a["id"] for a in anchors)
    for index, row in enumerate(rows or []):
        if not isinstance(row, dict):
            out.append(f"anchor-premise-malformed-row: rows[{index}] (not a mapping)")
            continue
        named = _named(row)
        if named is not None and not known.intersection(_expand(named, known)):
            out.append(f"anchor-premise-unknown: {named} (no declared anchor has this id)")
    claims = {a["id"]: a["claim"] for a in anchors}
    if verdicts is None:
        verdicts = anchor_verdicts(anchors, rows, carry_rows, plan_text, repo_root, base_ref)
    for entry in verdicts:
        verdict = entry["verdict"]
        if verdict is None:
            if entry["observed"]:
                continue
            out.append(f"anchor-premise-no-verdict: {entry['id']}")
        elif verdict != "RESOLVES":
            out.append(
                f"anchor-premise-{verdict}: {entry['id']} -- {str(claims[entry['id']])[:_CLAIM_CAP]}"
            )
    return out
