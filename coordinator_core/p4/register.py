"""
coordinator_core/p4/register.py — ``p4.register_workspace`` op (C7).

Spec backlink: docs/plans/2026-09-12-perforce-second-class-commit-and-shelve.md
§ C7, D1, D9.

Classified MUTATING (``coordinator_core.authz.classification``). Writes the
machine-local ``p4.<repo_key>.{port,user,client,client_root}`` identity row
(``registry_set`` — untracked, machine-local only), and separately writes the
repo-true ``vcs_mirror: p4`` marker, the ``p4_repo_key`` cross-repo read
surface (F6 — in-repo lookups resolve via ``session_change._resolve_repo_key``'s
reverse scan instead; see that inline comment below), plus the two optional
``p4_submit_tool``/``p4_checkout_tool`` slots into the repo's
``coordinator.local.md`` frontmatter (tracked, repo-true — never identity).
It also authors ``.p4ignore`` (adds a ``.git/`` line so p4 never depot-adds
the parallel git repo this op creates) and a UE-derived-artifact
``.gitignore`` (D2a's second containment layer), and append-only pins
``* -text`` into ``.gitattributes``.

Called by DoE's H4 ``/repo-setup`` skill step, which does not reimplement
this — this op is the one documented stable entry point for ANY surface that
registers a p4 workspace (cockpit's desktop registration affordance calls it
too; this signature is a cross-repo contract, not an internal detail).

Review: overengineering-reviewer F2 (integrator-applied — documentation
fix, params kept). ``p4_submit_tool``/``p4_checkout_tool`` are written but
read by NOTHING in this repo, BY DESIGN — they are a cross-repo contract
row, not residue. The reader contract is DoE's committed
``coordinator/contract/p4-provider-fragment.md`` (doe-claude-bc landed
``repo_key`` alongside ``changelist`` on the submit slot at DoE
``a05d95ba8``); DoE's ``finishing-a-development-branch`` H5 skill step
reads the submit slot to call the tool directly, and claude-unreal-
Example-game-repo's C12 is the work that fills the tool in. Neither reader lives in
this repo, so a grep here finding only register.py itself, its docstring,
and its test is expected, not evidence of dead code.

This is NOT a provider registry (D9 still holds — see
``write_guards/p4_checkout_before_edit.py``'s own deny text): a registry
would resolve/dispatch to a tool at runtime from these slots, and nothing
here does that. A named tool SLOT, written once at registration time for
an external caller to read later, is a narrower thing than a registry —
the two are not in tension despite looking related.

The route cockpit's TypeScript desktop app spawns to reach this op is the
engine's command-type dispatch CLI (DR-215 left this in place of the retired
resident daemon), never an import or a socket::

    python3 -m coordinator_core.invoke p4.register_workspace '<json params>'

Negative-spec:
  - ``repo_key`` is validated SHAPE ONLY (single slash, lowercase) — never
    derived from a directory, stream, or client name; refuses rather than
    minting one.
  - Never writes identity (port/user/client/client_root) into a tracked file — those
    four fields land ONLY in the machine-local registry.
  - ``.gitattributes``' line-ending pin is append-only: appends ``* -text``
    only when no conflicting text/eol rule is already present in the file;
    refuses loudly on a conflict rather than rewriting, and states the
    renormalization consequence for existing history in its own result
    message rather than silently pinning it — a UE depot repo is exactly the
    population likely to already carry LFS declarations in this file
    (``push_outstanding``'s own zero-spawn LFS-filter check reads the same
    file). Review: eng-director finding 15.
"""

from __future__ import annotations

import os
import re
import socket
from pathlib import Path
from typing import Optional

from coordinator_core.ipc import register_op
from coordinator_core.machine_resolver import registry_set
from coordinator_core.p4 import runner

#: D9 — shape-only: exactly one lowercase '<segment>/<segment>' pair. Never a
#: regex match against a derived/multi-segment/uppercase key.
_REPO_KEY_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*/[a-z0-9][a-z0-9_-]*$")

#: A qualified MCP tool name — "<server>__<tool>". Absent is legal (means the
#: slot stays empty); present-but-malformed refuses.
_QUALIFIED_TOOL_RE = re.compile(r"^[A-Za-z0-9_]+__[A-Za-z0-9_]+$")

#: D2a's UE derived-artifact set — the parallel git repo's own .gitignore
#: must exclude every one of these so D4's `git diff` path set never names
#: them.
_DERIVED_DIRS = ("Intermediate/", "Saved/", "DerivedDataCache/", "Binaries/")

#: A line in .gitattributes matching this is a conflicting text/eol rule —
#: the append-only pin refuses loudly rather than rewriting past it.
_GITATTRS_CONFLICT_RE = re.compile(r"(?<![\w-])(text\b|eol=)")

#: ``runner.run`` always passes ``-s``, which tags every output line
#: (``info: Root:\tE:\ws``, ``info: \tE:\alt``); the spec fields sit behind it.
_SCRIPT_TAG_RE = re.compile(r"^info\d*: ?")


class P4RegisterError(Exception):
    """A loud, fail-closed refusal (bad repo_key shape, client Root/Host
    mismatch, a conflicting .gitattributes rule, a malformed tool name) —
    the op handler folds this into ``{"ok": False, "error": str(exc)}``
    rather than letting it propagate as a bare exception."""


def _validate_repo_key(repo_key: object) -> str:
    if not isinstance(repo_key, str) or not _REPO_KEY_RE.match(repo_key):
        raise P4RegisterError(
            f"repo_key {repo_key!r} must be exactly one lowercase "
            "'<segment>/<segment>' pair (shape-only check — never derived "
            "from a directory, stream, or client name; a derived or "
            "multi-segment key is refused, not minted)"
        )
    return repo_key


def _validate_tool_name(name: Optional[object], field: str) -> Optional[str]:
    if name is None:
        return None
    if not isinstance(name, str) or not _QUALIFIED_TOOL_RE.match(name):
        raise P4RegisterError(
            f"{field} {name!r} must be a qualified MCP tool name "
            "('<server>__<tool>') or omitted — absent is legal and leaves "
            "the slot empty"
        )
    return name


def _parse_client_spec(stdout: str) -> dict:
    """Best-effort parse of ``p4 client -o <client>`` output for the three
    fields registration confirms against: ``Root``, ``AltRoots`` (each on
    its own tab-indented continuation line under the ``AltRoots:`` header),
    and ``Host``. Accepts ``-s`` script-tagged lines and untagged spec text
    alike."""
    root: Optional[str] = None
    alt_roots: list = []
    host: Optional[str] = None
    in_alt_roots = False
    for raw_line in stdout.splitlines():
        line = _SCRIPT_TAG_RE.sub("", raw_line, count=1)
        if line.startswith("Root:"):
            root = line[len("Root:"):].strip()
            in_alt_roots = False
        elif line.startswith("AltRoots:"):
            in_alt_roots = True
            rest = line[len("AltRoots:"):].strip()
            if rest:
                alt_roots.append(rest)
        elif line.startswith("Host:"):
            host = line[len("Host:"):].strip()
            in_alt_roots = False
        elif in_alt_roots and line.startswith("\t"):
            candidate = line.strip()
            if candidate:
                alt_roots.append(candidate)
        elif line and not line.startswith("\t"):
            in_alt_roots = False
    return {"root": root, "alt_roots": alt_roots, "host": host}


def _root_contains(candidate_root: str, repo_root_resolved: Path) -> bool:
    try:
        candidate = Path(candidate_root).resolve()
    except OSError:
        return False
    try:
        repo_root_resolved.relative_to(candidate)
        return True
    except ValueError:
        pass
    if repo_root_resolved == candidate:
        return True
    # Review: code-reviewer F2 (integrator-applied) -- `Path.resolve()` does
    # not reliably case-normalize a component that doesn't already exist
    # verbatim under that casing on disk, and a `p4 client -o` `Root:`/
    # `AltRoots:` value routinely differs in casing from what the OS
    # reports for the git repo directory (hand-authored/migrated client
    # specs vs. tool-cloned repos). `os.path.normcase` lowercases on
    # Windows and is identity on POSIX, so this widens acceptance only
    # where the filesystem is itself case-insensitive -- containment
    # semantics are unchanged (a sibling directory sharing a name prefix is
    # still refused by the boundary check below); this is a comparison
    # fix, not a loosening of what counts as contained.
    candidate_norm = os.path.normcase(os.path.normpath(str(candidate)))
    repo_norm = os.path.normcase(os.path.normpath(str(repo_root_resolved)))
    if repo_norm == candidate_norm:
        return True
    return repo_norm.startswith(candidate_norm + os.sep)


def _confirm_client(port: str, user: str, client: str, repo_root: str) -> str:
    """Resolves the client spec once via ``p4 client -o``, confirms
    Root/AltRoots contains the repo and Host matches this machine, and
    returns the client's OWN root — never a probe, never a `.p4config`
    walk-up; the one explicit ``-p/-u/-c`` spawn this op makes.

    The returned root is the matched Root/AltRoot verbatim, NOT ``repo_root``.
    The check below is ``contains``, not ``equals``: a client whose Root is a
    parent directory holding several projects passes it while differing from
    the git repo root, which is an ordinary Perforce layout. Those are two
    distinct facts and they get two distinct registry rows — see the
    ``registry_set`` calls in the handler.
    """
    result = runner.run(port, user, client, ["client", "-o"])
    if not result.ok:
        raise P4RegisterError(
            f"p4 client -o {client} failed to confirm the client spec: "
            f"{result.error}"
        )
    spec = _parse_client_spec(result.stdout)
    repo_root_resolved = Path(repo_root).resolve()
    candidates = [c for c in ([spec["root"]] + spec["alt_roots"]) if c]
    if not candidates or not any(
        _root_contains(c, repo_root_resolved) for c in candidates
    ):
        raise P4RegisterError(
            f"p4 client {client!r} Root/AltRoots ({candidates!r}) does not "
            f"contain repo_root {repo_root_resolved}"
        )
    matched_root = next(
        c for c in candidates if _root_contains(c, repo_root_resolved)
    )
    host = spec["host"]
    this_host = socket.gethostname()
    if host and host.lower() != this_host.lower():
        raise P4RegisterError(
            f"p4 client {client!r} Host ({host!r}) does not match this "
            f"machine ({this_host!r})"
        )
    return matched_root


def _ensure_p4ignore(repo_root: str) -> dict:
    """Ensures ``.p4ignore`` contains a ``.git/`` line so p4 never depot-adds
    the parallel git repo this op creates. Returns whether the file existed
    BEFORE this write (drives the .gitignore seed and the P4IGNORE/`-a`
    recording below) and its pre-existing text.

    Never rewrites a file that already carries ``.git/``: a depot-tracked
    ``.p4ignore`` is read-only under ``noallwrite``, and one that already
    ignores the git repo needs no checkout. One that lacks it refuses --
    this op never opens a depot file for edit on the operator's behalf."""
    p4ignore_path = Path(repo_root) / ".p4ignore"
    existed = p4ignore_path.is_file()
    prior_text = p4ignore_path.read_text(encoding="utf-8") if existed else ""
    lines = prior_text.splitlines()
    if ".git/" in lines:
        return {"existed": existed, "prior_text": prior_text, "path": str(p4ignore_path)}
    lines.append(".git/")
    try:
        p4ignore_path.write_text(
            "\n".join(lines) + "\n", encoding="utf-8", newline="\n"
        )
    except PermissionError as exc:
        raise P4RegisterError(
            f"{p4ignore_path} is read-only (depot-tracked) and lacks '.git/'. "
            "Run `p4 edit .p4ignore`, add a '.git/' line, then re-register."
        ) from exc
    return {"existed": existed, "prior_text": prior_text, "path": str(p4ignore_path)}


def _author_gitignore(repo_root: str, seed_text: str) -> str:
    """Authors ``.gitignore`` covering the UE derived set, seeded from the
    workspace's own pre-existing ``.p4ignore`` (D2a's second containment
    layer)."""
    gitignore_path = Path(repo_root) / ".gitignore"
    lines = [ln for ln in seed_text.splitlines() if ln.strip()]
    for derived in _DERIVED_DIRS:
        if derived not in lines:
            lines.append(derived)
    gitignore_path.write_text(
        "\n".join(lines) + "\n", encoding="utf-8", newline="\n"
    )
    return str(gitignore_path)


def _refuse_gitattributes_conflict(repo_root: str) -> str:
    """Raises on an existing text/eol rule; returns the prior text. Split out
    so the handler refuses before its first write, not after the .p4ignore
    and .gitignore writes have landed."""
    gitattrs_path = Path(repo_root) / ".gitattributes"
    prior_text = gitattrs_path.read_text(encoding="utf-8") if gitattrs_path.is_file() else ""
    for line in prior_text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if _GITATTRS_CONFLICT_RE.search(stripped):
            raise P4RegisterError(
                f".gitattributes already carries a text/eol rule ({stripped!r}) "
                "— refusing to append '* -text' rather than rewrite it. "
                "Pin the line-ending rule by hand if this repo's existing "
                "history should be renormalized."
            )
    return prior_text


def _pin_gitattributes(repo_root: str, prior_text: str) -> dict:
    """Append-only line-ending pin over ``prior_text``, which
    ``_refuse_gitattributes_conflict`` has already cleared; states the
    renormalization consequence for existing history in its returned message
    either way."""
    gitattrs_path = Path(repo_root) / ".gitattributes"
    if "* -text" in prior_text.splitlines():
        message = "'.gitattributes' already pins '* -text'; left unchanged."
    else:
        new_text = prior_text
        if new_text and not new_text.endswith("\n"):
            new_text += "\n"
        new_text += "* -text\n"
        gitattrs_path.write_text(new_text, encoding="utf-8", newline="\n")
        message = (
            "Appended '* -text' to .gitattributes. This renormalizes line "
            "endings for every file in FUTURE commits only — existing "
            "history is unaffected until an operator explicitly runs "
            "`git add --renormalize .` and commits the result; do that "
            "knowingly, not as a side effect of registration."
        )
    return {"path": str(gitattrs_path), "message": message}


def _upsert_local_md_keys(repo_root: str, key_values: dict) -> str:
    """Upserts flat top-level frontmatter keys into ``coordinator.local.md``,
    preserving every other line verbatim. Creates the file (with a bare
    frontmatter block) if absent. Never writes identity fields here — only
    repo-true marker/slot keys."""
    local_md_path = Path(repo_root) / "coordinator.local.md"
    if local_md_path.is_file():
        text = local_md_path.read_text(encoding="utf-8")
    else:
        text = "---\n---\n"
    lines = text.splitlines()
    if len(lines) < 2 or lines[0] != "---" or "---" not in lines[1:]:
        # No parseable frontmatter block — wrap the existing content under a
        # fresh one rather than guessing at repair.
        lines = ["---", "---"] + lines
    close_idx = lines[1:].index("---") + 1
    fm_lines = lines[1:close_idx]
    remaining = dict(key_values)
    for i, line in enumerate(fm_lines):
        for key in list(remaining.keys()):
            if line.startswith(f"{key}:"):
                fm_lines[i] = f"{key}: {remaining.pop(key)}"
                break
    for key, value in remaining.items():
        fm_lines.append(f"{key}: {value}")
    new_lines = ["---"] + fm_lines + lines[close_idx:]
    local_md_path.write_text(
        "\n".join(new_lines) + "\n", encoding="utf-8", newline="\n"
    )
    return str(local_md_path)


@register_op("p4.register_workspace")
def _register_workspace(params: dict, repo_root: Optional[Path] = None) -> dict:
    """JSON-RPC ``p4.register_workspace`` handler.

    Params: ``repo_key`` (required, shape-validated), ``repo_root`` (falls
    back to the dispatch-resolved repo root), ``port``/``user``/``client``
    (fall back to ambient ``P4PORT``/``P4USER``/``P4CLIENT`` — the one
    legitimate ambient-env read in this package, since resolving a fresh
    P4CONFIG is this op's whole job), ``p4_submit_tool``/``p4_checkout_tool``
    (optional, validated qualified-tool-name shape).
    """
    try:
        repo_key = _validate_repo_key(params.get("repo_key"))
        submit_tool = _validate_tool_name(params.get("p4_submit_tool"), "p4_submit_tool")
        checkout_tool = _validate_tool_name(params.get("p4_checkout_tool"), "p4_checkout_tool")

        resolved_repo_root = str(params.get("repo_root") or repo_root or os.getcwd())
        port = params.get("port") or os.environ.get("P4PORT")
        user = params.get("user") or os.environ.get("P4USER")
        client = params.get("client") or os.environ.get("P4CLIENT")
        if not (port and user and client):
            raise P4RegisterError(
                "port/user/client must be supplied explicitly or resolvable "
                "from ambient P4PORT/P4USER/P4CLIENT (the caller's own "
                "resolved P4CONFIG) — registration is the one place this "
                "engine reads ambient p4 env, exactly once"
            )

        client_root = _confirm_client(port, user, client, resolved_repo_root)

        # Every refusal and every repo-file write precedes the first
        # registry_set: a refusal part-way must never leave a half-registered
        # workspace, while the file writes are idempotent on a re-run.
        gitattrs_prior_text = _refuse_gitattributes_conflict(resolved_repo_root)
        p4ignore_state = _ensure_p4ignore(resolved_repo_root)
        gitignore_path = _author_gitignore(resolved_repo_root, p4ignore_state["prior_text"])
        gitattrs_result = _pin_gitattributes(resolved_repo_root, gitattrs_prior_text)

        if p4ignore_state["existed"]:
            ignore_message = (
                f".p4ignore pre-existed at {p4ignore_state['path']} — recorded "
                "for P4IGNORE."
            )
        else:
            ignore_message = (
                ".p4ignore did not pre-exist (this op authored one containing "
                "only '.git/') — recorded so the D2a runner drops '-a' rather "
                "than relying on it for full ignore semantics."
            )

        # Review: overengineering-reviewer F6 (integrator-applied --
        # documentation fix, both derivations kept). `p4_repo_key` is the
        # read surface for a CROSS-REPO consumer that cannot run the
        # reverse scan below (example-game-repo hit exactly this wall and correctly
        # refused to derive a key of its own) -- it belongs here rather
        # than in the machine-local registry because the key needed to
        # READ that registry cannot live inside it, and because the minted
        # key is the same identity on every box. In-repo lookups do NOT
        # read this frontmatter key -- they resolve via
        # `session_change._resolve_repo_key`'s reverse scan over the
        # `p4.<repo_key>.repo_root` rows, which is the live local path.
        local_md_keys = {"vcs_mirror": "p4", "p4_repo_key": repo_key}
        if submit_tool is not None:
            local_md_keys["p4_submit_tool"] = submit_tool
        if checkout_tool is not None:
            local_md_keys["p4_checkout_tool"] = checkout_tool
        local_md_path = _upsert_local_md_keys(resolved_repo_root, local_md_keys)

        registry_set(f"p4.{repo_key}.port", port)
        registry_set(f"p4.{repo_key}.user", user)
        registry_set(f"p4.{repo_key}.client", client)
        # Two facts, two rows. `.client_root` is the CLIENT's root — that is what the
        # cross-repo p4-provider contract's read-surface table labels it, and
        # what example-game-repo takes identity from to open its own p4 connection.
        # `.repo_root` is this git repo. They coincide only when the client
        # maps exactly one project; a Root that is a parent directory is an
        # ordinary layout, and collapsing them silently hands a provider the
        # wrong directory in precisely that case (both being real paths, it
        # would never surface as an error).
        registry_set(f"p4.{repo_key}.client_root", client_root)
        registry_set(f"p4.{repo_key}.repo_root", resolved_repo_root)
        if p4ignore_state["existed"]:
            registry_set(f"p4.{repo_key}.p4ignore_path", p4ignore_state["path"])
        else:
            registry_set(f"p4.{repo_key}.p4ignore_absent", "true")

        return {
            "ok": True,
            "repo_key": repo_key,
            "client": client,
            "port": port,
            "root": resolved_repo_root,
            "p4ignore": p4ignore_state["path"],
            "p4ignore_message": ignore_message,
            "gitignore": gitignore_path,
            "gitattributes": gitattrs_result["message"],
            "coordinator_local_md": local_md_path,
        }
    except P4RegisterError as exc:
        return {"ok": False, "error": str(exc)}
