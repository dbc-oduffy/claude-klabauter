"""coordinator_core.ops.post_coverage_status -- status-poster for the merge
gate's remote-authority layer (C1, docs/plans/2026-08-27-the-merge-gate-gets-
a-remote-authority-layer.md).

Purpose: map `gate.validate_invocable`'s "review" dimension verdict onto a
GitHub commit status (`repos/<owner>/<repo>/statuses/<sha>`, context
`coverage-gate`), so a raw `git push origin main` that bypasses every local
gate at least surfaces on the remote. This module authors NO coverage logic
of its own -- it is a pure CONSUMER of the already-existing verdict producer
(`coordinator/bin/merge-gate-and-pr.py`'s `_run_gate_validate_invocable`,
loaded here by file path the same way that file's own test suite already
does -- see `_load_merge_gate_module()`).

Verdict mapping is a WHITELIST, not a blacklist (Review: the Staff Engineer F1, cited in
the dispatch brief): only `review["verdict"] == "PASS"` maps to
`state=success`. Every other outcome -- `FAIL`, `ERROR`, `UNAVAILABLE`,
`SKIPPED`, a missing `review` key, or a `_changed_files` git failure (a
non-zero `proc.returncode`, checked explicitly rather than trusting empty
stdout alone) -- maps to `state=failure` with a one-fact `description` naming
which case fired. A blacklist silently re-opens this hole the next time
`Verdict` grows a member; a whitelist cannot.

NAKED PYTHON OVER HTTPS -- no `gh`, no subprocess of any kind. `gh` is not
named in docs/reference/shell-out-carve-outs.md's closed list, and that list
states its own membership rule: a site satisfying a class's rationale but not
named is a violation, not a carve-out. `urllib.request` against
api.github.com is both the compliant route and the cheaper one -- a
subprocess is exactly the process-creation cost DR-344's brightline charges
for.

TOKEN RESOLUTION, in order, never a shell-out (`gh auth token` is itself a
barred subprocess):
    1. `GITHUB_TOKEN` environment variable.
    2. `GH_TOKEN` environment variable.
    3. gh's own on-disk credential file (`gh config` never invoked -- this is
       a file read, which spawns nothing): `$GH_CONFIG_DIR/hosts.yml` if that
       env var is set, else `%APPDATA%/GitHub CLI/hosts.yml` on Windows or
       `$XDG_CONFIG_HOME/gh/hosts.yml` (default `~/.config/gh/hosts.yml`)
       elsewhere, reading the `oauth_token` under the `github.com` host entry.
    4. Windows Credential Manager, via `advapi32!CredReadW` through `ctypes`
       -- an in-process DLL call, not a subprocess.

Leg 4 is not belt-and-braces; without it the ladder does not reach this
fleet's actual credential. A `gh auth login` that chose SECURE STORAGE --
the default on Windows, and what this box has -- writes NO `oauth_token`
into `hosts.yml` at all. The file still exists and still parses, so leg 3
returns cleanly and wrongly: the host entry is present, the token key simply
is not there. Observed on this box 2026-08-28: `gh auth status` reports
`Logged in to github.com account dbc-example-operator (keyring)`, `hosts.yml` carries
only `users:`/`user:`, and legs 1-3 all miss while a valid `gho_` token sits
in the credential store under `gh:github.com:<user>`. That is the whole
fail-closed path firing on a credential that IS present -- which, once the
ruleset is live, refuses every push to `main` with no route through.

POSIX secure storage is the SAME hole, unpatched here: gh on Linux/macOS
also keyrings its token by default (Secret Service / Keychain), and reaching
either without a subprocess is a separate mechanism, not a line of code.
On those hosts the operator must export `GITHUB_TOKEN` or `GH_TOKEN`.
Leg 4 is Windows-only by construction and returns None everywhere else.

No token resolving is FAIL CLOSED: `post_coverage_status()` posts nothing and
returns a result recording that fact -- never "assume covered".

DISPOSITION TABLE (the load-bearing contract; see the dispatch brief for the
decision citations):
    verdict PASS                              -> POST state=success
    verdict FAIL/ERROR/UNAVAILABLE/SKIPPED,
    missing `review` key, or a `_changed_files`
    git failure (indeterminate-but-postable)   -> POST state=failure,
                                                   description names which
                                                   case fired
    no token resolves, or the POST itself gets
    403/429 (unpostable)                       -> post NOTHING; this is
                                                   forced by missing
                                                   capability, not a policy
                                                   choice

RATE-LIMIT DISCIPLINE: this module never retries into a 403/429 and never
treats the shared per-token bucket as private headroom -- it reads
`X-RateLimit-Remaining` off the response for the caller's own budgeting but
does not hammer on exhaustion; see `_post_status()`.

Budget note (DR-344 brightline, docs/wiki/machine-load-norm.md): a network
round trip is not itself a measured axis of either document -- both are
silent on "network"/"HTTP". The in-process work around the POST (request
construction, TLS handshake, response parsing) IS process time and is
budgeted the same as any other call; no number is invented for wire latency
itself. Measure process time and spawn count for this call site, not wall
clock -- the verdict computation this module consumes is the expensive part
and is a separate chunk's declared precondition, not this module's job.

No ambient override: no environment variable or module-level flag disables
the fail-closed paths above.

Spec backlink: docs/plans/2026-08-27-the-merge-gate-gets-a-remote-authority-
layer.md § C1.
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Optional

STATUS_CONTEXT = "coverage-gate"

_GH_KEYRING_SERVICE = "gh:github.com"
_CRED_TYPE_GENERIC = 1

_BIN_DIR = Path(__file__).resolve().parents[2] / "coordinator" / "bin"
_MERGE_GATE_MODULE_NAME = "merge_gate_and_pr_for_post_coverage_status"

_merge_gate_mod = None


def _load_merge_gate_module():
    """Load `coordinator/bin/merge-gate-and-pr.py` by file path.

    Same technique that file's own test suite already uses
    (`coordinator/bin/tests/test_merge_gate_and_pr.py::_load_module`) --
    the hyphenated filename is not import-able as a normal package member.
    Cached at module scope: this module never authors a second verdict
    computation, it only needs the one function `_run_gate_validate_invocable`
    and the one function `_changed_files` that already live there.
    """
    global _merge_gate_mod
    if _merge_gate_mod is not None:
        return _merge_gate_mod
    spec = importlib.util.spec_from_file_location(
        _MERGE_GATE_MODULE_NAME, _BIN_DIR / "merge-gate-and-pr.py"
    )
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    assert spec.loader is not None
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    _merge_gate_mod = mod
    return mod


# ---------------------------------------------------------------------------
# Token resolution -- env first, then gh's own credential file. Never a
# shell-out (`gh auth token` is itself barred).
# ---------------------------------------------------------------------------

def _gh_hosts_path() -> Path:
    override = os.environ.get("GH_CONFIG_DIR")
    if override:
        return Path(override) / "hosts.yml"
    if sys.platform == "win32":
        appdata = os.environ.get("APPDATA")
        if appdata:
            return Path(appdata) / "GitHub CLI" / "hosts.yml"
        return Path.home() / "AppData" / "Roaming" / "GitHub CLI" / "hosts.yml"
    xdg = os.environ.get("XDG_CONFIG_HOME")
    if xdg:
        return Path(xdg) / "gh" / "hosts.yml"
    return Path.home() / ".config" / "gh" / "hosts.yml"


def _gh_github_host_entry() -> Optional[dict]:
    """Parse gh's `hosts.yml` and return its `github.com` mapping, or None.

    Returns None -- never raises -- for every way the file can fail to yield
    one: absent, unreadable, invalid YAML, not a mapping, or carrying no
    `github.com` host entry.
    """
    path = _gh_hosts_path()
    if not path.is_file():
        return None
    import yaml

    try:
        with path.open("r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
    except (OSError, yaml.YAMLError):
        return None
    if not isinstance(data, dict):
        return None
    host_entry = data.get("github.com")
    if not isinstance(host_entry, dict):
        return None
    return host_entry


def _token_from_gh_hosts_file() -> Optional[str]:
    host_entry = _gh_github_host_entry()
    if host_entry is None:
        return None
    token = host_entry.get("oauth_token")
    if isinstance(token, str) and token:
        return token
    return None


def _gh_active_account() -> Optional[str]:
    """The account name gh records as active for `github.com`, or None.

    Names the Credential Manager target leg 4 reads. `user:` is gh's own
    active-account key; the first key under `users:` is the fallback for a
    hosts.yml written before that key existed.
    """
    host_entry = _gh_github_host_entry()
    if host_entry is None:
        return None
    user = host_entry.get("user")
    if isinstance(user, str) and user:
        return user
    users = host_entry.get("users")
    if isinstance(users, dict):
        for name in users:
            if isinstance(name, str) and name:
                return name
    return None


def _decode_credential_blob(blob: bytes) -> Optional[str]:
    """Decode a Credential Manager blob to a token, or None.

    Two writers, two encodings, and the wrong guess is silent: gh's own Go
    keyring writes the token as raw UTF-8, while several Windows credential
    writers store UTF-16LE. Try UTF-8 first and fall back on the NUL bytes
    that give UTF-16 away, then require the result to be printable -- a
    mis-decoded blob must fail closed, never travel to an Authorization
    header as mojibake.
    """
    for encoding in ("utf-8", "utf-16-le"):
        try:
            text = blob.decode(encoding).strip()
        except (UnicodeDecodeError, ValueError):
            continue
        if text and text.isprintable():
            return text
    return None


def _token_from_windows_credential_manager(*, platform: str = sys.platform) -> Optional[str]:
    """Read gh's keyring-stored token via `advapi32!CredReadW`, or None.

    An in-process DLL call through `ctypes` -- no subprocess, so nothing here
    needs a shell-out carve-out. Returns None on every non-Windows host and
    on every failure mode (DLL unavailable, target absent, blob undecodable,
    a malformed target or ABI mismatch surfacing as `ctypes.ArgumentError`),
    so the caller's fail-closed contract is unchanged.

    `platform` is injectable (Review: coordinatorcode-reviewer Finding 1) so
    tests can exercise the non-Windows branch without mutating the
    process-wide `sys.platform` singleton.
    """
    if platform != "win32":
        return None

    import ctypes
    import ctypes.wintypes as wintypes

    class _Credential(ctypes.Structure):
        _fields_ = [
            ("Flags", wintypes.DWORD),
            ("Type", wintypes.DWORD),
            ("TargetName", wintypes.LPWSTR),
            ("Comment", wintypes.LPWSTR),
            ("LastWritten", wintypes.FILETIME),
            ("CredentialBlobSize", wintypes.DWORD),
            ("CredentialBlob", ctypes.POINTER(ctypes.c_char)),
            ("Persist", wintypes.DWORD),
            ("AttributeCount", wintypes.DWORD),
            ("Attributes", ctypes.c_void_p),
            ("TargetAlias", wintypes.LPWSTR),
            ("UserName", wintypes.LPWSTR),
        ]

    try:
        advapi32 = ctypes.WinDLL("advapi32")
        advapi32.CredReadW.argtypes = [
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            ctypes.POINTER(ctypes.POINTER(_Credential)),
        ]
        advapi32.CredReadW.restype = wintypes.BOOL
        advapi32.CredFree.argtypes = [ctypes.c_void_p]
        advapi32.CredFree.restype = None
    except (OSError, AttributeError):
        return None

    account = _gh_active_account()
    targets = [f"{_GH_KEYRING_SERVICE}:{account}"] if account else []
    targets.append(f"{_GH_KEYRING_SERVICE}:")

    for target in targets:
        pointer = ctypes.POINTER(_Credential)()
        try:
            ok = advapi32.CredReadW(
                target, _CRED_TYPE_GENERIC, 0, ctypes.byref(pointer)
            )
        except (OSError, ctypes.ArgumentError):
            # Review: coordinatorcode-reviewer Finding 2 -- a malformed target
            # or ABI mismatch raises ctypes.ArgumentError, not OSError; catch
            # both so leg 4 matches this module's "fails closed, never raises"
            # docstring claim rather than propagating out of resolve_token().
            continue
        if not ok or not pointer:
            continue
        try:
            credential = pointer.contents
            size = int(credential.CredentialBlobSize)
            if size <= 0:
                continue
            blob = ctypes.string_at(credential.CredentialBlob, size)
        finally:
            advapi32.CredFree(pointer)
        token = _decode_credential_blob(blob)
        if token:
            return token
    return None


def resolve_token() -> Optional[str]:
    """Resolve a GitHub token, env first, then gh's on-disk credential file.

    Returns None if nothing resolves -- the caller MUST fail closed on that,
    never fabricate or assume a token.
    """
    for env_name in ("GITHUB_TOKEN", "GH_TOKEN"):
        value = os.environ.get(env_name)
        if value:
            return value
    return _token_from_gh_hosts_file() or _token_from_windows_credential_manager()


# ---------------------------------------------------------------------------
# Verdict -> status mapping (whitelist).
# ---------------------------------------------------------------------------

def _changed_files_or_git_failure(commit_range: str) -> tuple[list[str], bool]:
    """The ONE git spawn this module issues for changed-file listing --
    mirrors `merge-gate-and-pr.py::_changed_files`'s exact command, but
    additionally surfaces `proc.returncode` rather than folding a git
    failure into "no changed files" the way that function's stdout-only
    read does (the brief's named fix, Review: the Staff Engineer F1). This does not
    call `mod._changed_files` afterwards -- that would be a second spawn
    for the same information, which is exactly the kind of duplication
    DR-344's spawn-count budget charges for.

    Returns (changed_files, git_failed).
    """
    import subprocess

    from coordinator_core.win_portability import no_console_creationflags

    proc = subprocess.run(
        ["git", "diff", "--name-only", commit_range],
        capture_output=True,
        text=True,
        check=False,
        **no_console_creationflags(),
    )
    if proc.returncode != 0:
        return [], True
    return [line for line in proc.stdout.splitlines() if line], False


def compute_status(commit_range: str, repo_root: Optional[str] = None) -> tuple[str, str]:
    """Return (state, description) per the disposition table in the module
    docstring. `state` is one of "success"/"failure". Never raises for an
    indeterminate verdict -- only a WHITELIST match (`verdict == "PASS"`)
    reaches `state=success`.
    """
    mod = _load_merge_gate_module()
    root = repo_root or os.getcwd()

    changed_files, git_failed = _changed_files_or_git_failure(commit_range)
    if git_failed:
        return "failure", "coverage-gate: git diff failed while listing changed files"

    if not changed_files:
        # No changed files is a real, benign case upstream (`cmd_coverage_gate`
        # exits 0 for it) -- but this poster's whitelist requires an actual
        # earned PASS verdict, not an absence of input, so this still reads
        # as an indeterminate case rather than a silent success.
        return "failure", "coverage-gate: no changed files in range; nothing was measured"

    result = mod._run_gate_validate_invocable(changed_files, commit_range, root)
    dimensions = {d["dimension"]: d for d in result.get("dimensions", [])}
    review = dimensions.get("review")
    if review is None:
        return "failure", "coverage-gate: review dimension absent from gate.validate_invocable result"

    verdict = review.get("verdict")
    if verdict == "PASS":
        return "success", review.get("detail", "coverage-gate: covered")
    return "failure", f"coverage-gate: verdict {verdict!r} -- {review.get('detail', '')}"[:140]


# ---------------------------------------------------------------------------
# POST to api.github.com -- naked urllib, no subprocess.
# ---------------------------------------------------------------------------

class PostResult:
    """Outcome of a `post_coverage_status()` call. `posted` is False for
    every fail-closed path (no token, rate-limited, HTTP error) -- a caller
    must never read `posted=False` as `state=success`."""

    def __init__(self, posted: bool, state: Optional[str], reason: str):
        self.posted = posted
        self.state = state
        self.reason = reason

    def to_json(self) -> dict:
        return {"posted": self.posted, "state": self.state, "reason": self.reason}


def _post_status(
    owner: str,
    repo: str,
    sha: str,
    state: str,
    description: str,
    token: str,
) -> PostResult:
    url = f"https://api.github.com/repos/{owner}/{repo}/statuses/{sha}"
    payload = json.dumps(
        {
            "state": state,
            "description": description[:140],
            "context": STATUS_CONTEXT,
        }
    ).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=payload,
        method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "Content-Type": "application/json",
            "User-Agent": "claude-klabauter-coverage-gate-poster",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            resp.read()
            remaining = resp.headers.get("X-RateLimit-Remaining")
            reason = "posted"
            if remaining is not None:
                reason = f"posted (rate-limit remaining={remaining})"
            return PostResult(posted=True, state=state, reason=reason)
    except urllib.error.HTTPError as exc:
        if exc.code in (403, 429):
            # Fail closed on rate-limit exhaustion -- an INDETERMINATE case,
            # never retried, never degraded to "assume covered".
            return PostResult(
                posted=False,
                state=None,
                reason=f"unpostable: HTTP {exc.code} from statuses API (rate-limited)",
            )
        return PostResult(posted=False, state=None, reason=f"unpostable: HTTP {exc.code}")
    except urllib.error.URLError as exc:
        return PostResult(posted=False, state=None, reason=f"unpostable: {exc.reason}")


def post_coverage_status(
    owner: str,
    repo: str,
    sha: str,
    commit_range: str,
    repo_root: Optional[str] = None,
) -> PostResult:
    """Compute the coverage-gate verdict for `commit_range` and POST it as a
    commit status on `sha`. Fails closed (posts nothing) when no token
    resolves -- never posts `state=success` on a missing capability.
    """
    token = resolve_token()
    if not token:
        return PostResult(
            posted=False,
            state=None,
            reason="unpostable: no GitHub token resolved (GITHUB_TOKEN/GH_TOKEN env, gh hosts.yml)",
        )
    state, description = compute_status(commit_range, repo_root=repo_root)
    return _post_status(owner, repo, sha, state, description, token)


# ---------------------------------------------------------------------------
# CLI entry point.
# ---------------------------------------------------------------------------

def main(argv: list[str]) -> int:
    import argparse

    parser = argparse.ArgumentParser(prog="post_coverage_status")
    parser.add_argument("--owner", required=True)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--sha", required=True)
    parser.add_argument("--commit-range", default="main..HEAD")
    args = parser.parse_args(argv)

    result = post_coverage_status(args.owner, args.repo, args.sha, args.commit_range)
    print(json.dumps(result.to_json()))
    return 0 if result.posted else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
