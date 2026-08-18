"""
coordinator_core.session.session_facts — the session fact facade.

Roadmap `fact-layer-core`, stub `fl-core-01` chunk C2
(docs/plans/2026-08-18-session-fact-facade-and-failure-posture.md). Builds against
docs/decisions/DR-319-session-fact-facade-shape-and-failure-posture.md, which is
BINDING here, not advisory — this module's return shape is DR-319's contract verbatim,
checked by this package's own contract test rather than by prose review.

SHAPE — a plain Python module, NOT a registered op (DR-319 § (a)). The engine is
warm-on-demand with cold spawn-per-call as the fallback (CLAUDE.md § What this repo
is); a registered op costs one engine invocation per fact read, a leaf-module import
costs zero. `fl-core-02` lifts four more facts onto whatever shape this decision picks,
multiplying the choice by five — a registered op multiplies a real per-call cost by
five before any measurement is taken, a leaf module multiplies zero by five. Same
precedent shape as `coordinator_core/composition_budget.py`: "dependency-free leaf"
there means free of the ENGINE (no dispatch, no op registration, no subprocess to check
a budget) — it does not mean this module may not import the producer whose fact it
fronts, which is the entire point of a facade. This module imports intra-repo
producers only (`branch_resolution.session_commit_count_attributed`,
`branch_resolution._read_session_shape`) and nothing from `coordinator_core.ipc` or
the op registry.

SERVES THREE FACTS SO FAR (`fl-core-02` C2, C3): the attributed session commit
magnitude (`session_magnitude_attributed`), the session pickup kind
(`session_pickup_kind`), and the session diff brightline
(`session_diff_brightline`). C4–C5 and C7b (`fl-core-02`'s remaining rows) lift the
other two close-gate facts onto this same module — anti-scope for THIS chunk only, not
a standing limit on the module.

RETURN-SHAPE CONTRACT (DR-319, binding — verbatim, not summarized):
    Discriminator key `"degraded"` (bool), present on every returned record.

    Computed (`degraded: False`):
        {"degraded": False, "value": <fact payload>, "source": "<producer string>",
         "collision": <bool | None>}
    `collision` is ALWAYS present on a computed record — `None` when the fact has no
    peer-mutable surface, a bool when it does. Never omitted, never coerced to `False`
    (R-11): an omitted key declares nothing, which is the absent-vs-clean conflation
    R-11 forbids; `None` is the fact's own declaration that no collision mode exists.

    Degraded (`degraded: True`):
        {"degraded": True, "evidence": "<free prose>", "source": "<producer string>"}
    No `"value"` key on a degraded record — a caller reading `.get("value")` on a
    degraded record gets `None` by absence, never a fabricated zero.

    NO verdict/recommendation/disposition/action key anywhere (AC8, DR-306's
    detect/decide split) — this facade emits evidence and collision state, never a
    decision replacing an EM judgment.

WHY `collision: None` FOR THIS FACT (R-11's peer-mutability test, not fact identity):
`session_commit_count_attributed` scopes strictly to commits carrying THIS session's own
`Session-Id: <sid>` trailer. A peer session cannot write a commit under a foreign sid —
sid assignment is per-session — so no peer mutation can change what this fact reports
for a given sid, even though the underlying `git log` surface (the whole branch history)
is itself shared. This is R-11's Fact-1 precedent (session-scoped, single-writer): the
fact has no peer-mutable surface, so it declares `collision: None` rather than an
always-`False` field that would read as "checked, clean."

KNOWN ATTRIBUTION LIMIT — DECLARED, NOT SILENTLY INHERITED (the Staff Engineer F5, R-11): the
backing producer's `--grep=Session-Id: <sid>` only sees commits carrying the trailer.
A commit made via plumbing (`git commit-tree`, bypassing the porcelain `commit` path)
loses the trailer, so a computed `value: 0` is reachable BOTH for a session that made
no commits AND for a session that committed real work entirely through plumbing. This
facade cannot recover that distinction — no signal exists to tell the two cases apart —
so it is declared here, at the served fact's own seam, rather than left implicit.  A
caller that needs to rule this out must independently check for un-trailered commits;
this fact's `value: 0` does not do so on its own.

ANTI-SCOPE (this chunk only, not a standing rule): does not build a second session
store (`coordinator_core/session/shape.py` already is one; this module reads through
existing producers, never a store of its own — AC10). Does not lift Facts 2, 4, 5
(`fl-core-02`'s remaining rows). Does not converge the 21 hand-rolled dirty-probe
implementations (a sibling roadmap-seed's scope, not this one's). Does not edit
`quick_wrap_assemble/__init__.py` — DR-323 § (a)'s coexistence-then-cut discipline
makes C7 the only chunk that cuts the interim readers over; this module's facts are
served ALONGSIDE them until then.

Spec backlink: docs/plans/2026-08-18-session-fact-facade-and-failure-posture.md § C2
Contract:      docs/decisions/DR-319-session-fact-facade-shape-and-failure-posture.md
Authoritative: state/roadmap/fact-layer-core/COORDINATOR-RESOLUTIONS.md (R-09, R-10, R-11)
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from coordinator_core.ops.ceremony.branch_resolution import (
    _BRIGHTLINE_COMMITS,
    _BRIGHTLINE_LOC,
    _BRIGHTLINE_SURFACES,
    SCOPING_METHOD_AMBIGUOUS,
    _git_run,
    _read_session_shape,
    _session_surface_count,
    _session_touched_paths,
    analyze_session_scoping,
    session_commit_count_attributed,
)

#: Literal, grep-able backing-source string (DR-319 § (b)) — a reader can grep this
#: straight back to the producing function, not a category token.
_SOURCE_SESSION_MAGNITUDE_ATTRIBUTED = (
    "coordinator_core/ops/ceremony/branch_resolution.py::session_commit_count_attributed"
)

#: Same convention, for Fact 1's producer (DR-319 § (b), DR-323 body).
_SOURCE_SESSION_PICKUP_KIND = (
    "coordinator_core/ops/ceremony/branch_resolution.py::_read_session_shape"
)

#: Same convention, for Fact 3's producer. Points at `session_commit_count_attributed`
#: specifically — DR-323 § (b)'s table names this fact's backing source as "trailer-
#: attributed to this sid (`git log --grep=Session-Id: <sid>`)", which is exactly that
#: function's own call, and it is the ONE sub-read whose degraded state this composed
#: fact propagates verbatim (module docstring below, "WHY `session_diff_brightline`
#: DEGRADES"). The other sub-reads composed into this fact (`_novel_loc_split`'s own
#: `git log --numstat`, `_session_touched_paths`, `analyze_session_scoping`) are
#: reported under the same source string rather than a second field — DR-319 § (b)
#: declares one backing-source string per served fact, not one per sub-read.
_SOURCE_SESSION_DIFF_BRIGHTLINE = (
    "coordinator_core/ops/ceremony/branch_resolution.py::session_commit_count_attributed"
)

#: Local re-declaration of `quick_wrap_assemble`'s `_KIND_RE` (kept private, not
#: imported): the facade must not depend on `quick_wrap_assemble` — that dependency
#: runs the other direction post-C7 (DR-323 § (a), coexistence-then-cut) — and this is
#: a two-line regex, not infrastructure worth a shared module over.
_KIND_RE = re.compile(r"^kind:\s*['\"]?([A-Za-z_-]+)['\"]?\s*$", re.MULTILINE)

#: Local re-declarations of `quick_wrap_assemble`'s doc-only carve-out, for the same
#: dependency-direction reason as `_KIND_RE` above: `_novel_loc_split` and the surface
#: carve-out both need them, and the facade must not import from `quick_wrap_assemble`.
_DOC_ONLY_SUFFIXES: frozenset[str] = frozenset({".md", ".markdown", ".rst", ".txt"})
_DOC_ONLY_PREFIXES: tuple[str, ...] = (
    "docs/",
    "state/",
    "archive/",
    "cross-repo/",
    "tasks/",
)


def _read_frontmatter_kind(path: Path) -> str | None:
    """Read the `kind:` frontmatter scalar off `path`, tolerant of a malformed body.

    Deliberately a regex over the first document, not a YAML parse — mirrors
    `quick_wrap_assemble._read_frontmatter_field`'s own rationale, re-declared here
    rather than imported for the same reason as `_KIND_RE` above.
    """
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    head = text.split("\n---", 2)[0] if text.startswith("---") else text[:4096]
    match = _KIND_RE.search(head)
    return match.group(1).strip() if match else None


def session_magnitude_attributed(worktree_root: Path, sid: str) -> dict:
    """Serve the attributed session commit magnitude: the count of commits on
    `worktree_root`'s branch carrying a `Session-Id: <sid>` trailer for THIS session,
    via `git log --grep=Session-Id: <sid>` — never `git rev-list --count
    head_at_start..HEAD` (AC4's trap: the positional count credits a session with its
    peers' commits on a shared branch, per R-09).

    Returns DR-319's return-shape contract (module docstring, "RETURN-SHAPE CONTRACT"):
      - computed:  {"degraded": False, "value": <int>, "source": <str>, "collision": None}
      - degraded:  {"degraded": True, "evidence": <str>, "source": <str>}

    `collision` is always `None` for this fact — see module docstring, "WHY
    `collision: None` FOR THIS FACT".

    KNOWN LIMIT: `value: 0` is reachable both for a genuinely zero-commit session and
    for a session that committed only via plumbing (loses the `Session-Id:` trailer) —
    see module docstring, "KNOWN ATTRIBUTION LIMIT". This facade does not (cannot)
    disambiguate the two; it serves the trailer-attributed count, declared as such.
    """
    record = session_commit_count_attributed(worktree_root, sid)
    if record["degraded"]:
        return {
            "degraded": True,
            "evidence": record["evidence"],
            "source": _SOURCE_SESSION_MAGNITUDE_ATTRIBUTED,
        }
    return {
        "degraded": False,
        "value": record["value"],
        "source": _SOURCE_SESSION_MAGNITUDE_ATTRIBUTED,
        "collision": None,
    }


def session_pickup_kind(worktree_root: Path, common_dir: Path, sid: str) -> dict[str, Any]:
    """Serve the picked-up artifact's classification for THIS session
    (`fl-core-02` Fact 1; DR-323's lift of `quick_wrap_assemble._read_pickup_kind`).

    `value["classification"]` is one of `handoff` / `spinoff` / `memo` / `none`,
    resolved from the artifact's own `kind:` frontmatter when the picked-up file is
    still readable, and from the session-shape record alone when it is not (a handoff
    archived mid-session still classifies, it does not fall to `none`).

    AC6 — THE MIXED HANDOFF-PLUS-MEMO CASE, AND ITS PRECEDENCE STATED HERE: a session
    can both consume a handoff AND action memos in the same sitting. This is not data
    loss — `value["actioned_memos"]` always carries the full list regardless of which
    way `classification` resolves. `classification` itself stays single-valued and
    PICKS THE HANDOFF OVER THE MEMOS whenever a handoff pickup happened; it reports
    `"memo"` only on the branch where no handoff pickup happened AND at least one memo
    was actioned. A caller that needs the memo axis when a handoff also happened reads
    `value["actioned_memos"]` directly — it is never dropped to make room for the
    single-valued field.

    Returns DR-319's return-shape contract (module docstring, "RETURN-SHAPE CONTRACT"):
      - computed:  {"degraded": False, "value": <dict, below>, "source": <str>,
                    "collision": None}
      - degraded:  {"degraded": True, "evidence": <str>, "source": <str>}

    `value`'s keys, on a computed record: `classification`, `artifact_path`,
    `basename`, `deliverable_id`, `actioned_memos`, `consumed_predecessor` — the same
    fields `_read_pickup_kind` already computed; only the posture and the enclosing
    shape changed here, not the payload.

    `collision` is always `None` for this fact (DR-323 § (b) table): session-shape.json
    is session-scoped and single-writer, the same no-peer-surface reasoning R-11
    applies to `session_magnitude_attributed` above — no peer session can mutate
    another session's own pickup record.

    POSTURE CONVERSION (AC3): `_read_session_shape` itself never raises — it catches
    `json.JSONDecodeError`/`OSError` internally and returns `({}, "absent")`, which is
    ALSO what a session that has never had a session-shape.json written at all returns.
    Those two are not the same fact: one is "nothing to read yet" (a legitimately
    computed `classification: "none"`), the other is "a file exists and could not be
    parsed" (a genuine probe failure). `_read_session_shape` collapses that distinction
    before it reaches this facade, so this function recovers it the only way available
    without editing that producer: checking whether the backing path exists. Source
    `"absent"` with the path PRESENT on disk means the read failed after the file was
    written — degraded, with evidence naming the producer, the call, and the file path.
    Source `"absent"` with the path MISSING is the ordinary "no session-shape.json yet"
    case — computed, `classification: "none"`, same as today.
    """
    shape_path = common_dir / "coordinator-sessions" / sid / "session-shape.json"
    shape, source = _read_session_shape(common_dir, sid)

    if source == "absent" and shape_path.exists():
        return {
            "degraded": True,
            "evidence": (
                "branch_resolution.py::_read_session_shape(common_dir, sid) returned "
                f"source=\"absent\" for {shape_path}, which exists on disk — its own "
                "try/except around json.load caught a JSONDecodeError or OSError and "
                "logged a warning rather than propagating it, so the file could not be "
                "parsed even though it is present."
            ),
            "source": _SOURCE_SESSION_PICKUP_KIND,
        }

    pickup = shape.get("pickup") or {}
    history = shape.get("pickup_history") or []
    actioned_memos = shape.get("actioned_memos") or []

    basename = (pickup.get("handoff") or "").strip()
    happened = bool(pickup.get("happened")) and bool(basename)

    if not happened and history:
        for entry in reversed(history):
            if entry.get("happened") and entry.get("handoff"):
                basename = str(entry["handoff"]).strip()
                happened = True
                break

    if not happened:
        value = {
            "classification": "memo" if actioned_memos else "none",
            "artifact_path": None,
            "basename": None,
            "deliverable_id": None,
            "actioned_memos": [dict(m) for m in actioned_memos],
            "consumed_predecessor": bool(actioned_memos),
        }
    else:
        artifact_path = None
        kind = None
        for directory in ("state/handoffs", "archive/handoffs"):
            candidate = worktree_root / directory / basename
            if candidate.exists():
                artifact_path = f"{directory}/{basename}"
                kind = _read_frontmatter_kind(candidate)
                break

        value = {
            "classification": (kind or "handoff").strip().lower(),
            "artifact_path": artifact_path,
            "basename": basename,
            "deliverable_id": pickup.get("deliverable_id"),
            "actioned_memos": [dict(m) for m in actioned_memos],
            "consumed_predecessor": True,
        }

    return {
        "degraded": False,
        "value": value,
        "source": _SOURCE_SESSION_PICKUP_KIND,
        "collision": None,
    }


# ---------------------------------------------------------------------------
# session_diff_brightline (fl-core-02 C3) — lift of
# quick_wrap_assemble._read_diff / _novel_loc_split.
# ---------------------------------------------------------------------------


def _resolve_rename_path(path: str) -> str:
    """Reduce a `--numstat -M` path field to the file's CURRENT path.

    Moved wholesale from `quick_wrap_assemble/__init__.py :: _resolve_rename_path`
    (fl-core-02 C3) — logic unchanged, see that module's git history for the rename-
    parsing rationale (`old => new` and the `pre/{old_mid => new_mid}/file.ext`
    compressed form both resolve to the NEW path before any suffix/prefix rule runs).
    """
    text = path.strip()
    if "=>" not in text:
        return text
    if "{" in text and "}" in text:
        head, _, rest = text.partition("{")
        middle, _, tail = rest.partition("}")
        _old, _, new_mid = middle.partition("=>")
        combined = f"{head}{new_mid.strip()}{tail}"
        # `{a => b}` with an empty side collapses to a doubled separator.
        return combined.replace("//", "/").strip()
    _old, _, new = text.partition("=>")
    return new.strip()


def _classify_path(path: str) -> str:
    """`code` or `doc`. Doc-only paths are carved out of the novel LOC/surface counts.

    Moved wholesale from `quick_wrap_assemble/__init__.py :: _classify_path`
    (fl-core-02 C3) — logic unchanged.
    """
    normalized = _resolve_rename_path(path).replace("\\", "/").strip()
    if not normalized:
        return "code"
    if Path(normalized).suffix.lower() in _DOC_ONLY_SUFFIXES:
        return "doc"
    if normalized.startswith(_DOC_ONLY_PREFIXES):
        return "doc"
    return "code"


def _novel_loc_split(worktree_root: Path, sid: str) -> dict[str, Any]:
    """Split this session's changes into gross / doc-only / relocated / novel, for both
    lines AND commits. One `git log --numstat -M` pass, not three.

    Moved wholesale from `quick_wrap_assemble/__init__.py :: _novel_loc_split`
    (fl-core-02 C3) — the parsing logic (`-M`'s "moved unmodified" carve-out,
    `novel_loc = gross_loc - doc_only_loc` floored at 0, `novel_commit_count` applying
    the same doc-only carve-out to the commit axis) is unchanged; only the git-failure
    posture changed, per this chunk's job.

    NOT ITSELF A DR-319 RECORD — a private sub-read `session_diff_brightline` folds
    into its own DR-319 shape. Uses `branch_resolution._git_run` (returncode-checked),
    never `quick_wrap_assemble._git_out` (swallows a git failure into `""`
    indistinguishable from a genuinely-empty diff — the exact fail-open posture this
    lift retires): `{"degraded": False, **counts}` on success,
    `{"degraded": True, "evidence": <str>}` when the underlying `git log` call fails.
    """
    result = _git_run(
        [
            "log",
            "--numstat",
            "-M",
            "--format=@@QWA-COMMIT@@ %H",
            f"--grep=Session-Id: {sid}",
        ],
        worktree_root,
    )
    if result.returncode != 0:
        return {
            "degraded": True,
            "evidence": (
                f"git log --numstat -M --grep=Session-Id: {sid} failed: returncode="
                f"{result.returncode!r} stderr={result.stderr.strip()!r}"
            ),
        }

    marker = "@@QWA-COMMIT@@ "
    gross = 0
    doc_only = 0
    relocated = 0
    novel_commits: set[str] = set()
    current_sha = ""
    for line in result.stdout.splitlines():
        if line.startswith(marker):
            current_sha = line[len(marker) :].strip()
            continue
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        added_raw, deleted_raw, path = parts[0], parts[1], parts[2]
        if added_raw == "-" or deleted_raw == "-":
            continue
        try:
            changed = int(added_raw) + int(deleted_raw)
        except ValueError:
            continue
        gross += changed
        if changed == 0:
            relocated += 1
            continue
        if _classify_path(path) == "doc":
            doc_only += changed
        elif current_sha:
            novel_commits.add(current_sha)
    novel = max(0, gross - doc_only)
    return {
        "degraded": False,
        "gross_loc": gross,
        "doc_only_loc": doc_only,
        "relocated_files": relocated,
        "novel_loc": novel,
        "novel_commit_count": len(novel_commits),
    }


def session_diff_brightline(worktree_root: Path, common_dir: Path, sid: str) -> dict[str, Any]:
    """Serve the session-scoped diff brightline (`fl-core-02` Fact 3; DR-323's lift of
    `quick_wrap_assemble._read_diff` / `_novel_loc_split`).

    Composes `analyze_session_scoping`'s `ScopingVerdict`, `_session_touched_paths`,
    `_session_surface_count`, `session_commit_count_attributed`, and this module's own
    `_novel_loc_split` (the one genuinely new piece, moved wholesale above).
    `_BRIGHTLINE_LOC`/`_BRIGHTLINE_COMMITS`/`_BRIGHTLINE_SURFACES` stay imported from
    `branch_resolution`, never restated, for the same reason `quick_wrap_assemble`'s own
    import comment states: so the review-partition verdict and this brightline cannot
    drift apart silently.

    Returns DR-319's return-shape contract (module docstring, "RETURN-SHAPE CONTRACT"):
      - computed:  {"degraded": False, "value": <dict, below>, "source": <str>,
                    "collision": None}
      - degraded:  {"degraded": True, "evidence": <str>, "source": <str>}

    WHY THIS FACT DEGRADES, AND ON WHAT (the fail-open this chunk retires): the OLD
    `_read_diff` called `session_commit_count_attributed` — already converted to
    DR-319's degraded-with-evidence shape by `session_magnitude_attributed`'s own
    producer — and then discarded the distinction: `commit_count = record["value"] if
    not record["degraded"] else 0`. A degraded probe and a genuinely zero-commit
    session both read as `commit_count: 0` at every downstream call site. This function
    propagates `session_commit_count_attributed`'s degraded state as a degraded FACT
    instead: if that sub-read is degraded, `session_diff_brightline` is degraded,
    full stop, before any of the other sub-reads run. The other git-backed sub-read
    this function OWNS — `_novel_loc_split`'s `git log --numstat` call — gets the same
    treatment for the same reason (see that function's own docstring): a fact that
    cannot compute its novel-LOC/commit split has no basis to report a brightline
    verdict either.

    `_session_touched_paths` and `analyze_session_scoping`'s internal `git log` calls
    are NOT converted here — both are existing producers with their own fail-open
    posture (`_session_touched_paths` returns `[]` on any git failure;
    `analyze_session_scoping`'s pipeline never raises, by its own docstring's design)
    that this chunk does not own and does not silently inherit past acknowledging it:
    a touched-paths read that failed reports the same `[]` as a session that touched
    nothing, so `surface_count`/`novel_surface_count`/`touched_paths` in a computed
    record carry this same known limit forward, undisturbed by this lift. Converging
    those producers' own posture is out of this chunk's scope (DR-323 names C3 as the
    diff-brightline lift, not a `_session_touched_paths` rewrite).

    THREE INDEPENDENT AXES TRAVEL ON THIS FACT — KEPT DISTINCT, STATED HERE SO THE
    NEXT READER DOES NOT REDISCOVER IT:
      - `degraded` (this docstring, above): could the probe run at all.
      - `collision` (DR-323 § (b) table, below): does the backing source have a
        peer-mutable surface. `None` for this fact.
      - `trustworthy` (`value["trustworthy"]`, DERIVED FROM `scoping_method`): is the
        `Session-Id` trailer a RELIABLE ATTRIBUTION SIGNAL for this sid, independent
        of whether the probe that read it succeeded. `scoping_method == "ambiguous-
        x-node"` means the trailer itself is not trustworthy — a foreign or non-
        contiguous commit history makes trailer-based scoping unsafe — which is NOT
        "the probe could not run" (that is `degraded`) and NOT "a peer can mutate this
        fact's source" (that is `collision`). Folding `trustworthy` into `degraded`
        would report an untrustworthy-but-successfully-computed read as a probe
        failure; folding it into `collision` would report a data-quality signal as a
        peer-contention signal. Both are the same class of conflation DR-319 forbids
        for `degraded`/`collision` themselves, recurring one axis over — a computed
        record with `trustworthy: false` is still `degraded: False`, still carries
        real (if lower-confidence) counts, and the caller reads `trustworthy` to
        decide how much weight to give them.

    `collision` is always `None` for this fact (DR-323 § (b) table): trailer
    attribution is scoped strictly to commits carrying THIS session's own
    `Session-Id: <sid>` trailer — a peer session cannot write a commit under a
    foreign sid, so no peer mutation can change what this fact reports for a given
    sid, even though the underlying `git log` surface (the whole shared branch
    history) is itself shared. Same no-peer-surface reasoning
    `session_magnitude_attributed` already carries for a sid-scoped read (module
    docstring, "WHY `collision: None` FOR THIS FACT").

    `value`'s keys, on a computed record: `scoping_method`, `trustworthy`,
    `sha_range`, `commit_count`, `surface_count`, `novel_surface_count`,
    `touched_paths`, `gross_loc`, `doc_only_loc`, `relocated_files`, `novel_loc`,
    `novel_commit_count`, `brightline` (the three threshold constants), `breached`
    (which brightline axes are at/over threshold), `under_brightline` — the same
    fields `_read_diff` already computed; only the posture and the enclosing shape
    changed here, not the payload. `breached`/`under_brightline` are a mechanical
    threshold comparison against fixed constants, not an EM judgment call — quick-
    wrap's own doctrine already computes this brightline (condition 2 of 4; only
    condition 4, "is the work finished," is genuine EM discretion) — so they are not
    AC8's forbidden verdict/recommendation/disposition/action shape.
    """
    commit_count_record = session_commit_count_attributed(worktree_root, sid)
    if commit_count_record["degraded"]:
        return {
            "degraded": True,
            "evidence": commit_count_record["evidence"],
            "source": _SOURCE_SESSION_DIFF_BRIGHTLINE,
        }

    split = _novel_loc_split(worktree_root, sid)
    if split["degraded"]:
        return {
            "degraded": True,
            "evidence": split["evidence"],
            "source": _SOURCE_SESSION_DIFF_BRIGHTLINE,
        }
    split = {k: v for k, v in split.items() if k != "degraded"}

    verdict = analyze_session_scoping(worktree_root, common_dir, sid)
    touched = _session_touched_paths(worktree_root, sid)
    surface_count = _session_surface_count(touched)
    novel_touched = [p for p in touched if _classify_path(p) == "code"]
    novel_surface_count = _session_surface_count(novel_touched)

    over = {
        "novel_loc": split["novel_loc"] >= _BRIGHTLINE_LOC,
        "novel_commit_count": split["novel_commit_count"] >= _BRIGHTLINE_COMMITS,
        "novel_surface_count": novel_surface_count >= _BRIGHTLINE_SURFACES,
    }
    breached = sorted(k for k, v in over.items() if v)
    trustworthy = verdict.method != SCOPING_METHOD_AMBIGUOUS

    value = {
        "scoping_method": verdict.method,
        "trustworthy": trustworthy,
        "sha_range": verdict.candidate_range or None,
        "commit_count": commit_count_record["value"],
        "surface_count": surface_count,
        "novel_surface_count": novel_surface_count,
        "touched_paths": touched,
        **split,
        "brightline": {
            "novel_loc": _BRIGHTLINE_LOC,
            "novel_commit_count": _BRIGHTLINE_COMMITS,
            "novel_surface_count": _BRIGHTLINE_SURFACES,
        },
        "breached": breached,
        "under_brightline": not breached,
    }

    return {
        "degraded": False,
        "value": value,
        "source": _SOURCE_SESSION_DIFF_BRIGHTLINE,
        "collision": None,
    }
