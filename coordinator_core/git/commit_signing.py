
from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import List, Optional, Tuple, Union

from coordinator_core.git.git_dir import resolve_git_common_dir
from coordinator_core.win_portability import no_console_creationflags


class SigningFailed(RuntimeError):
    pass


_TRUE_SPELLINGS = frozenset({"true", "yes", "on", "1"})
_FALSE_SPELLINGS = frozenset({"false", "no", "off", "0", ""})


def _strip_inline_comment(line: str) -> str:
    out = []
    in_quotes = False
    escaped = False
    for ch in line:
        if escaped:
            out.append(ch)
            escaped = False
            continue
        if ch == "\\":
            out.append(ch)
            escaped = True
            continue
        if ch == '"':
            in_quotes = not in_quotes
        elif ch in "#;" and not in_quotes:
            break
        out.append(ch)
    return "".join(out)


def _read_config_bool(config_path: Path, section: str, key: str) -> Optional[bool]:
    try:
        text = config_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    in_section = False
    found: Optional[bool] = None
    for line in text.splitlines():
        stripped = _strip_inline_comment(line).strip()
        if stripped.startswith("["):
            in_section = stripped.lower().startswith(f"[{section}]")
            continue
        if not in_section or not stripped:
            continue
        raw_key, sep, value = stripped.partition("=")
        if raw_key.strip().lower() != key:
            continue
        if not sep:
            found = True
            continue
        normalized = value.strip().strip('"').lower()
        if normalized in _TRUE_SPELLINGS:
            found = True
        elif normalized in _FALSE_SPELLINGS:
            found = False
        else:
            found = None
    return found


def commit_signing_enabled(root: Path) -> bool:
    """Whether `commit.gpgsign` is on for `root` -- resolved spawn-free from
    the same two config files `_resolve_commit_identity` reads, in git's own
    local-over-global precedence.

    Negative spec -- this is DELIBERATELY narrower than git's own resolution
    and a miss returns False, which is exactly today's behaviour. It must
    never spawn to resolve: the seams it guards are on the brightline budget.
    """
    for config_path in (
        resolve_git_common_dir(root) / "config",
        Path.home() / ".gitconfig",
    ):
        value = _read_config_bool(config_path, "commit", "gpgsign")
        if value is not None:
            return value
    return False


def sign_flag_args(root: Path) -> List[str]:
    return ["-S"] if commit_signing_enabled(root) else []


def write_signed_commit_object(
    repo: Union[str, Path],
    root_tree: str,
    old_head: Optional[str],
    message: Union[str, bytes],
    name: str,
    email: str,
    stamp: str,
) -> Tuple[Optional[str], Optional[str]]:
    env = dict(os.environ)
    for role in ("AUTHOR", "COMMITTER"):
        env[f"GIT_{role}_NAME"] = name
        env[f"GIT_{role}_EMAIL"] = email
        env[f"GIT_{role}_DATE"] = stamp
    args = ["git", "-C", str(repo), "commit-tree", "-S", root_tree]
    if old_head:
        args += ["-p", old_head]
    args += ["-F", "-"]
    raw = message if isinstance(message, bytes) else message.encode("utf-8", "surrogateescape")
    body = raw if raw.endswith(b"\n") else raw + b"\n"
    try:
        proc = subprocess.run(
            args,
            input=body,
            capture_output=True,
            timeout=30,
            env=env,
            **no_console_creationflags(),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return None, (
            f"commit.gpgsign is set but `git commit-tree -S` could not run: "
            f"{exc} -- committed unsigned instead"
        )
    if proc.returncode != 0:
        return None, (
            "commit.gpgsign is set but `git commit-tree -S` failed -- "
            f"committed unsigned instead: "
            f"{proc.stderr.decode('utf-8', 'replace').strip()}"
        )
    sha = proc.stdout.decode("utf-8", "replace").strip()
    if len(sha) < 40:
        return None, (
            "commit.gpgsign is set but `git commit-tree -S` returned "
            f"{sha!r}, not a commit sha -- committed unsigned instead"
        )
    return sha, None
