"""coordinator_core.pickup_gate_brief — the computed gate-brief fields of
`pickup-assemble brief`: `gates.gate_check.clearer`, `.evidence_probes[]`,
and `preflight.premise_drift[]`.

Contract: DoE `coordinator/docs/wiki/baton-lifecycle/gate-brief-computed-fields-contract.md`.
Principle: the EM never inputs what the engine can compute.

Every function here is read-only and never raises: a source that cannot be
read degrades to the field's own "not known" shape, never to a guess.

Budget: `clearer` and `evidence_probes` are spawn-free (frontmatter plus the
machine-local registry, read lazily). `premise_drift` is at most three git
spawns per brief whatever the token count (one `cat-file --batch-check`, one
`log -1` for the authoring commit, one bounded range `log`), none when the baton
cites no commit. The contract's `git log -S`
pickaxe is not used: it measured 390ms for 50 commits on this repo, past the
500ms bar for any baton older than a day, so a later commit counts only when
its message names the cited token.

Negative spec: nothing here runs a probe, re-pins a SHA, rewrites a baton, or
flips a gate. A `probe-command` is carried with a `run_line`, never executed;
a `probe-op-key` is carried too, because running an arbitrary registered op
inside the brief is not a read.
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path
from typing import Any, Optional

CLEARER_HUMAN_PM = "human-pm"
CLEARER_PEER = "peer"
CLEARER_EXTERNAL = "external"

_PROBE_KINDS = ("probe-op-key", "probe-command")
_NOTE_TOKEN_RE = re.compile(r"[A-Za-z0-9_-]+")


def _repo_registry() -> dict[str, str]:
    """`repos.<key>` -> path from the machine-local registry; local overlay wins."""
    try:
        from coordinator_core import machine_resolver as mr

        reg_dir = mr.registry_dir()
        out: dict[str, str] = {}
        for fname in ("registry.toml", "registry.local.toml"):
            for key, val in mr.load_flat_registry_file(reg_dir / fname).items():
                if key.startswith("repos.") and "." not in key[6:] and isinstance(val, str) and val.strip():
                    out[key[6:]] = val.strip()
        return out
    except Exception:
        return {}


def _same_path(a: str, b: str) -> bool:
    return os.path.normcase(os.path.abspath(a)) == os.path.normcase(os.path.abspath(b))


def _own_repo_keys(registry: dict[str, str], root: Path) -> set[str]:
    return {key for key, path in registry.items() if _same_path(path, str(root))}


def _gate_notes_text(fm: dict[str, Any]) -> str:
    for key in ("gate_notes", "blocking_notes"):
        value = fm.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _legs(gate_evidence: Any) -> list[dict[str, Any]]:
    legs = gate_evidence.get("legs") if isinstance(gate_evidence, dict) else None
    return [leg for leg in legs if isinstance(leg, dict)] if isinstance(legs, list) else []


def compute_clearer(
    root: Path, fm: dict[str, Any], blockers: list[dict[str, Any]], gate_evidence: Any
) -> dict[str, Any]:
    """`gates.gate_check.clearer` — `{class, basis, repo?, matched?}`, first
    matching rule wins (contract § 1). A leg's repo only counts as foreign
    when this repo's own registry key is known; an unregistered own repo
    falls through to the human-judgment default rather than guessing."""
    if any(b.get("status") == "resolved" for b in blockers):
        return {"class": CLEARER_PEER, "basis": "blocked_by"}

    legs = _legs(gate_evidence)
    if any(leg.get("kind") == "human" for leg in legs):
        return {"class": CLEARER_HUMAN_PM, "basis": "gate_evidence.human"}

    registry: Optional[dict[str, str]] = None
    own: set[str] = set()
    foreign_repo: Optional[str] = None
    for leg in legs:
        if leg.get("kind") == "sibling-commitment-ref":
            foreign_repo = str(leg.get("repo") or "")
            break
        repo = leg.get("repo")
        if isinstance(repo, str) and repo:
            if registry is None:
                registry = _repo_registry()
                own = _own_repo_keys(registry, root)
            if own and repo not in own:
                foreign_repo = repo
                break
    if foreign_repo is not None:
        out: dict[str, Any] = {"class": CLEARER_EXTERNAL, "basis": "gate_evidence.foreign-repo"}
        if foreign_repo:
            out["repo"] = foreign_repo
        return out

    text = _gate_notes_text(fm)
    if text:
        if registry is None:
            registry = _repo_registry()
            own = _own_repo_keys(registry, root)
        names = {}
        for key in registry:
            if key not in own:
                names[key] = key
                names[key.replace("_", "-")] = key
        for token in _NOTE_TOKEN_RE.findall(text):
            if token in names:
                return {"class": CLEARER_EXTERNAL, "basis": "gate_notes.registry-match", "matched": token}
    return {"class": CLEARER_HUMAN_PM, "basis": "gate_notes.by-construction"}


def _clearer_phrase(clearer: dict[str, Any]) -> str:
    cls = clearer["class"]
    if cls == CLEARER_PEER:
        return "a peer baton"
    if cls == CLEARER_EXTERNAL:
        named = clearer.get("repo") or clearer.get("matched")
        return f"the sibling repo {named}" if named else "a sibling repo"
    return "the human PM"


def jgate_question(clearer: dict[str, Any]) -> str:
    return f"Has this awaiting_gate handoff's gate actually cleared? Clearer: {clearer['class']} ({_clearer_phrase(clearer)})."


def jgate_guidance(clearer: dict[str, Any]) -> tuple[str, str]:
    """`(cleared, not_cleared)` guidance specific to the clearer class."""
    cls = clearer["class"]
    if cls == CLEARER_PEER:
        cleared = "Cite the peer's closing artifact (a gates.gate_check.blockers entry, or its shipped_in)."
        settle = "the peer's closing artifact"
    elif cls == CLEARER_EXTERNAL:
        cleared = "Cite the sibling's landed work (its commit, or its commitment record)."
        settle = "the sibling's landed work"
    else:
        cleared = "Name the act that cleared it in gate_cleared_by; the PM's own act, not an inference."
        settle = "the PM's act, recorded in gate_cleared_by"
    return cleared, f"Settled by {settle}; none is on the record. Leave awaiting_gate; re-run pickup-assemble after."


def fail_closed_recommendation(
    clearer: dict[str, Any], probes: list[dict[str, Any]]
) -> dict[str, str]:
    """Recommendation for a gate carrying `gate_notes` and no clearing
    evidence: `not-cleared`, naming the clearer and what would settle it."""
    settle = jgate_guidance(clearer)[1].split(";")[0].removeprefix("Settled by ")
    rationale = f"gate_notes names the gate; clearer is {_clearer_phrase(clearer)}; no clearing evidence on the record. Settles on {settle}."
    if probes:
        ids = ", ".join(str(p.get("leg_id")) for p in probes)
        rationale += f" Probe evidence declared, not a clear: {ids}."
    return {"disposition": "not-cleared", "rationale": rationale}


def _render_run_line(kind: str, repo: Any, ref: str, registry: dict[str, str]) -> str:
    """Host-correct invocation text; `""` when it cannot be rendered safely."""
    if kind == "probe-op-key":
        return f"coordinator-invoke {ref}"
    parts = ref.split(None, 1)
    if not parts:
        return ""
    script = parts[0].replace("\\", "/")
    if script.startswith("/") or re.match(r"^[A-Za-z]:", script) or ".." in script.split("/"):
        return ""
    base = registry.get(str(repo))
    if not base or not Path(base).is_dir():
        return ""
    target = os.path.normpath(os.path.join(base, *script.split("/")))
    args = parts[1] if len(parts) > 1 else ""
    if sys.platform == "win32":
        quoted = f'"{target}"' if " " in target else target
        interpreter = "python " if script.endswith(".py") else ""
    else:
        quoted = f"'{target}'" if " " in target else target
        interpreter = "python3 " if script.endswith(".py") else ""
    return f"{interpreter}{quoted} {args}".rstrip()


def compute_evidence_probes(gate_evidence: Any) -> list[dict[str, Any]]:
    """`gates.gate_check.evidence_probes[]` — one entry per declared
    `probe-op-key` / `probe-command` leg (contract § 3). `result` is always
    `None`: neither kind is run inside the brief."""
    legs = [leg for leg in _legs(gate_evidence) if leg.get("kind") in _PROBE_KINDS]
    if not legs:
        return []
    registry = _repo_registry() if any(leg["kind"] == "probe-command" for leg in legs) else {}
    probes = []
    for leg in legs:
        kind = leg["kind"]
        ref = str(leg.get("ref") or "")
        probes.append({
            "leg_id": leg.get("leg_id"),
            "kind": kind,
            "repo": leg.get("repo"),
            "ref": ref,
            "note": leg.get("note") or "",
            "trust": "engine-run" if kind == "probe-op-key" else "operator-confirm",
            "run_line": _render_run_line(kind, leg.get("repo"), ref, registry),
            "result": None,
        })
    return probes


# ---------------------------------------------------------------------------
# preflight.premise_drift
# ---------------------------------------------------------------------------

_SHA_RE = re.compile(r"(?<![0-9A-Za-z_])[0-9a-f]{7,40}(?![0-9A-Za-z_])")
_MAX_TOKENS = 64
_MAX_SUPERSEDING = 10
_MAX_PINBOARD = 5
_PINBOARD_REL = "state/orientation_cache.md"
_LOG_WINDOW = 400
_LOG_BUDGET_SECS = 1.0


def _candidate_tokens(fm_text: str, body: str) -> dict[str, str]:
    """token -> where first cited (`frontmatter` | `body`), first-seen order."""
    found: dict[str, str] = {}
    for where, text in (("frontmatter", fm_text), ("body", body)):
        for token in _SHA_RE.findall(text):
            if token not in found and len(found) < _MAX_TOKENS:
                found[token] = where
    return found


def _resolve_commits(root: Path, tokens: list[str]) -> dict[str, str]:
    """token -> full commit sha, unresolvable or ambiguous tokens dropped.
    One `cat-file --batch-check` spawn for all tokens."""
    from coordinator_core.git.run import run_git

    payload = "".join(f"{t}^{{commit}}\n" for t in tokens).encode()
    proc = run_git(["cat-file", "--batch-check=%(objectname) %(objecttype)"], cwd=str(root), input=payload)
    lines = proc.stdout.splitlines()
    if proc.returncode != 0 or len(lines) != len(tokens):
        return {}
    resolved = {}
    for token, line in zip(tokens, lines):
        sha, _, kind = line.partition(" ")
        if kind == "commit" and len(sha) == 40:
            resolved[token] = sha
    return resolved


def _superseding_commits(root: Path, rel: str, tokens: list[str]) -> dict[str, list[dict[str, str]]]:
    """token -> commits NEWER than the baton's last authoring commit whose
    message names that token (subject or body). Two spawns: `log -1 -- rel`
    for the authoring commit, then one `log <authoring>..HEAD` over at most
    `_LOG_WINDOW` commits. Empty when the baton is untracked (no authoring
    boundary, and a guess would report unrelated mentions as drift)."""
    from coordinator_core.git.run import run_git

    authoring = run_git(["log", "-1", "--format=%H", "--", rel], cwd=str(root)).stdout.strip()
    if len(authoring) != 40:
        return {}
    proc = run_git(
        ["log", f"-n{_LOG_WINDOW}", "--no-color", f"{authoring}..HEAD", "--format=%x01%H%x02%s%x02%b"],
        cwd=str(root), timeout=_LOG_BUDGET_SECS,
    )
    if proc.returncode != 0:
        return {}
    out: dict[str, list[dict[str, str]]] = {}
    for chunk in proc.stdout.split("")[1:]:
        sha, _, message = chunk.partition("")
        subject = message.split("", 1)[0].strip()
        for t in tokens:
            if t in message and len(out.setdefault(t, [])) < _MAX_SUPERSEDING:
                out[t].append({"sha": sha[:12], "subject": subject})
    return out


def _pinboard_lines(root: Path, tokens: list[str]) -> dict[str, list[str]]:
    try:
        text = (root / _PINBOARD_REL).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return {}
    hits: dict[str, list[str]] = {}
    for line in text.splitlines():
        for t in tokens:
            if t in line and len(hits.setdefault(t, [])) < _MAX_PINBOARD:
                hits[t].append(line.strip()[:300])
    return hits


def compute_premise_drift(root: Path, abs_path: Path, text: str) -> list[dict[str, Any]]:
    """`preflight.premise_drift[]` (contract § 4): the baton's cited commits
    that a later commit or the pinboard has moved on. Only drifted tokens are
    listed."""
    try:
        from coordinator_core.frontmatter.primitives import split_frontmatter

        split = split_frontmatter(text)
        fm_text, body = (split.fm_text, split.body_with_leading_newline) if split else ("", text)
        cited = _candidate_tokens(fm_text, body)
        if not cited:
            return []
        resolved = _resolve_commits(root, list(cited))
        if not resolved:
            return []
        tokens = list(resolved)
        later = _superseding_commits(root, abs_path.relative_to(root).as_posix(), tokens)
        pins = _pinboard_lines(root, tokens)
        return [
            {
                "cited": token, "resolved": resolved[token], "cited_at": cited[token],
                "superseding": later.get(token, []), "pinboard": pins.get(token, []),
            }
            for token in tokens
            if later.get(token) or pins.get(token)
        ]
    except Exception:
        return []
