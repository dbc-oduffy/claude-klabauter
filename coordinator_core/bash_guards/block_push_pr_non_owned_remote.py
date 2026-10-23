"""coordinator_core.bash_guards.block_push_pr_non_owned_remote -- PreToolUse(Bash,
PowerShell) hard deny: publish is the line. No shell command pushes to, or opens
a pull request against, a remote the box does not own.

Armed by the machine-local key `policy.non_owned_remotes`: a list of remote
prefixes (`github.com/<org>/`, `github.com/<org>/<repo>`, `<host>:<port>`).
Absent or empty: allow. An unreadable registry denies a push or PR command.

Remote URLs and entries are normalised before the prefix match: scheme, `user@`
and a trailing `.git` dropped, scp-style `host:path` turned into `host/path`,
everything lowercased. A match is a whole path segment (`github.com/o/r` does not
match `github.com/o/r2`).

Zero spawns: argv parse, one registry read per file, and a direct read of the
repo's git config (through a `.git` file's `gitdir:` pointer):
  - `git push` with no remote uses `branch.<current>.remote`, else `origin`;
    `git push <name>` uses `remote.<name>.url` and `pushurl`; a literal URL is
    used as is; a remote written as a variable or substitution denies;
  - `gh pr create` uses `--repo/-R OWNER/REPO` as `github.com/OWNER/REPO`, else
    the first present of the `upstream`, `github`, `origin` remotes.
`--dry-run`, fetch, pull and `gh pr view|list|checkout` are never denied. A
`url.<base>.insteadOf` rewrite is not applied.

No override: the PM made it a hard requirement.
"""

from __future__ import annotations

import os
import re
from typing import Any, Dict, List, Optional, Tuple

from coordinator_core.bash_guards._command_tokenizer import (
    resolve_command_positions,
    token_matches_binary,
)
from coordinator_core.bash_guards._tool_names import COMMAND_TOOL_NAMES
from coordinator_core.bash_guards.block_topic_branch import (
    _QUOTED_RE,
    _cd_target,
    _git_argv,
    _git_root,
    _head_branch,
    _push_positional,
)

CLASS = "hard-deny"
MATCHERS = COMMAND_TOOL_NAMES
PRIORITY = 44

POLICY_KEY = "policy.non_owned_remotes"
ANCHOR = "PUSH-AND-PR-TO-A-NON-OWNED-REMOTE-ARE-DENIED"

_PRE_FILTER_RE = re.compile(r"(?i)\b(?:push|pr)\b")
_GH_DEFAULT_REMOTES = ("upstream", "github", "origin")
_SCHEME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*://")
_SCP_RE = re.compile(r"^([^/:]+):(?!\d+(?:/|$))(.+)$")
_SECTION_RE = re.compile(r'^\[\s*([A-Za-z0-9.-]+)(?:\s+"((?:[^"\\]|\\.)*)")?\s*\]\s*$')
_UNREADABLE = object()


def _configured_prefixes() -> Any:
    """The declared prefix list, `None` when the key is absent everywhere, or `_UNREADABLE`."""
    from coordinator_core.machine_resolver import load_flat_registry_file, registry_dir

    try:
        reg_dir = registry_dir()
        for fname in ("registry.local.toml", "registry.toml"):
            flat = load_flat_registry_file(reg_dir / fname)
            if POLICY_KEY in flat:
                value = flat[POLICY_KEY]
                if isinstance(value, str):
                    value = [value]
                return [str(v) for v in value if str(v).strip()] if value else []
    except Exception:  # noqa: BLE001 -- an unreadable registry cannot prove the box unarmed
        return _UNREADABLE
    return None


def normalize_remote(url: str) -> str:
    """Remote URL or declared entry in comparison form."""
    text = url.strip()
    scheme = _SCHEME_RE.match(text)
    if scheme:
        text = text[scheme.end():]
    text = text.rsplit("@", 1)[-1] if "@" in text.split("/", 1)[0] else text
    if not scheme:
        m = _SCP_RE.match(text)
        if m:
            text = f"{m.group(1)}/{m.group(2).lstrip('/')}"
    text = text.lower()
    if text.endswith(".git"):
        text = text[: -len(".git")]
    return text


def _matches(remote: str, prefixes: List[str]) -> bool:
    norm = normalize_remote(remote)
    for entry in prefixes:
        pref = normalize_remote(entry)
        if not pref:
            continue
        if norm == pref or norm.rstrip("/") == pref.rstrip("/"):
            return True
        if norm.startswith(pref) and (pref.endswith("/") or norm[len(pref)] in "/:"):
            return True
    return False


def _read_config(git_root: Optional[str]) -> Dict[Tuple[str, str], Dict[str, List[str]]]:
    """`{(section, subsection): {key: [values]}}` from the repo's own config file."""
    if not git_root:
        return {}
    from coordinator_core.git.git_dir import resolve_git_common_dir

    out: Dict[Tuple[str, str], Dict[str, List[str]]] = {}
    try:
        path = os.path.join(str(resolve_git_common_dir(git_root)), "config")
        with open(path, encoding="utf-8", errors="replace") as fh:
            lines = fh.read().splitlines()
    except OSError:
        return out
    section: Optional[Tuple[str, str]] = None
    for line in lines:
        line = line.strip()
        if not line or line[0] in "#;":
            continue
        m = _SECTION_RE.match(line)
        if m:
            section = (m.group(1).lower(), m.group(2) or "")
            continue
        if section is None or "=" not in line:
            continue
        key, _, value = line.partition("=")
        out.setdefault(section, {}).setdefault(key.strip().lower(), []).append(
            value.strip().strip('"')
        )
    return out


def _remote_urls(config: Dict[Tuple[str, str], Dict[str, List[str]]], name: str) -> List[str]:
    entry = config.get(("remote", name))
    if not entry:
        return []
    return list(entry.get("url", [])) + list(entry.get("pushurl", []))


def _looks_like_url(spec: str) -> bool:
    return bool(_SCHEME_RE.match(spec)) or any(c in spec for c in ":/@")


def _hidden(tok: str) -> bool:
    return tok.startswith("$") or "$(" in tok or "`" in tok or tok.startswith("%")


def _push_remotes(args: List[str], git_root: Optional[str]) -> Optional[List[str]]:
    """URLs a `git push` goes to; `[""]` when the remote cannot be read; None for a dry run."""
    if any(t in ("-n", "--dry-run") for t in args):
        return None
    explicit = next((a.split("=", 1)[1] for a in args if a.startswith("--repo=")), None)
    if explicit is None:
        for i, tok in enumerate(args):
            if tok == "--repo" and i + 1 < len(args):
                explicit = args[i + 1]
    positional = _push_positional(args)
    spec = explicit if explicit is not None else (positional[0] if positional else None)
    config = _read_config(git_root)
    if spec is None:
        branch = _head_branch(git_root)
        names = config.get(("branch", branch or ""), {}).get("remote") or ["origin"]
        return [u for n in names for u in _remote_urls(config, n)]
    if _hidden(spec):
        return [""]
    urls = _remote_urls(config, spec)
    if urls:
        return urls
    return [spec] if _looks_like_url(spec) else []


def _gh_pr_create(tokens: List[str]) -> Optional[List[str]]:
    """Args after `gh pr create`, or None when this is not that command."""
    if not tokens or not token_matches_binary(tokens[0], "gh"):
        return None
    rest = tokens[1:]
    if len(rest) >= 2 and rest[0] == "pr" and rest[1] == "create":
        return rest[2:]
    return None


def _gh_remotes(args: List[str], git_root: Optional[str]) -> List[str]:
    repo: Optional[str] = None
    for i, tok in enumerate(args):
        if tok in ("-R", "--repo") and i + 1 < len(args):
            repo = args[i + 1]
        elif tok.startswith("--repo="):
            repo = tok.split("=", 1)[1]
        elif tok.startswith("-R") and len(tok) > 2:
            repo = tok[2:]
    if repo is not None:
        if _hidden(repo):
            return [""]
        return [repo if _looks_like_url(repo) and repo.count("/") != 1 else f"github.com/{repo}"]
    config = _read_config(git_root)
    for name in _GH_DEFAULT_REMOTES:
        urls = config.get(("remote", name), {}).get("url", [])
        if urls:
            return list(urls)
    return []


def _offence(
    tokens: List[str], cwd: Optional[str], prefixes: List[str]
) -> Optional[Tuple[str, str]]:
    """`(command, remote)` when the segment pushes or opens a PR toward a non-owned remote."""
    parsed = _git_argv(tokens)
    if parsed is not None and parsed[0] == "push":
        remotes = _push_remotes(parsed[1], _git_root(cwd, parsed[2]))
        command = "git push"
    else:
        gh_args = _gh_pr_create(tokens)
        if gh_args is None:
            return None
        remotes = _gh_remotes(gh_args, _git_root(cwd, []))
        command = "gh pr create"
    for remote in remotes or []:
        if remote == "":
            return command, "(remote not readable)"
        if _matches(remote, prefixes):
            return command, normalize_remote(remote)
    return None


def _deny_reason(command: str, remote: str) -> str:
    return (
        f"BLOCKED: `{command}` toward non-owned remote {remote}. {ANCHOR}. "
        "Alternative: commit locally and hand the push or PR to a human."
    )


def check(payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    if (payload.get("tool_name") or "") not in MATCHERS:
        return None
    tool_input = payload.get("tool_input") or {}
    cmd = (tool_input.get("command") if isinstance(tool_input, dict) else None) or ""
    if not cmd or not _PRE_FILTER_RE.search(cmd):
        return None
    cmd = cmd.replace("\r", "")
    cwd = payload.get("cwd")
    segments = resolve_command_positions(cmd, preserve_windows_backslashes=True)
    follow_cd = not any(c in _QUOTED_RE.sub("", cmd) for c in "()")
    found: Optional[Tuple[str, str]] = None
    prefixes: Any = None
    for resolved in segments:
        if follow_cd and resolved.depth == 0 and resolved.tokens[:1] == ["cd"]:
            cwd = _cd_target(cwd, resolved.tokens[1:])
            continue
        is_push = _git_argv(resolved.tokens)
        if not (is_push and is_push[0] == "push") and _gh_pr_create(resolved.tokens) is None:
            continue
        if prefixes is None:
            prefixes = _configured_prefixes()
            if prefixes is None or prefixes == []:
                return None
            if prefixes is _UNREADABLE:
                found = ("a push or PR command", "(registry unreadable)")
                break
        found = _offence(resolved.tokens, cwd, prefixes)
        if found:
            break
    if found is None:
        return None
    return _deny_record(*found)


def _deny_record(command: str, remote: str) -> Any:
    from coordinator_core.guard_messages.alternative_record import AlternativeRecord, EscalationRoute

    legacy = _deny_reason(command, remote)
    summary, _, _alt = legacy.partition(" Alternative: ")
    return AlternativeRecord(
        verdict_kind="deny",
        guard_name="block-push-pr-non-owned-remote",
        summary=summary,
        em_text=legacy,
        members=(
            EscalationRoute(
                report_what="the local commit the push or PR is for",
                authorize_what="a human pushing it or opening the PR",
            ),
        ),
    )
