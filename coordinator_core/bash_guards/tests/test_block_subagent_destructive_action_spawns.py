"""Smoke tests for coordinator_core.bash_guards.block_subagent_destructive_action.

Not a full golden-parity corpus (that's a later W3b/plan-AC2 deliverable) --
representative payloads exercising Layer 1 surface-gating, the DUAL
OR-resolver identity axis, the AMBIGUOUS fail-closed path, and a
representative slice of the Layer-2 git deny/allow ladder (including the
near-miss --force-with-lease boundary the recipe explicitly flags).

Uses `monkeypatch` on the module's `resolve_effective_types` import to avoid
needing a real git repo + back-pointer chain on disk for AMBIGUOUS/back-pointer
cases; PRIMARY-leg (`agent_type`) cases exercise the real resolver end-to-end
since they need no git_root at all.
"""

from __future__ import annotations

from pathlib import Path

import pytest


pytestmark = [pytest.mark.complete, pytest.mark.spawns_process, pytest.mark.cadence]


# ---------------------------------------------------------------------------
# Indirection-wrapper hardening (2026-07-21 addition -- see module docstring
# "INDIRECTION-WRAPPER HARDENING" and
# cross-repo/inbox/2026-07-20-claude-central-em-subagent-destructive-guard-
# indirection-bypass.md). Each wrapper shape gets a denied case; shapes this
# guard can RELIABLY parse (`-c <string>`, `env ... <cmd>`) also get a
# benign-passes case, to demonstrate the recurse-and-match path does not
# over-block content it can actually examine.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Indirection-advisory message shape (RESHAPE 2026-08-06, C18d -- see module
# docstring's new "RESHAPE" comment on `check()`). The prior B2/B3 "message
# honesty" tests pinned a long deny message's "Safe forward paths" guidance
# text; that guidance no longer exists because this shape no longer blocks
# at all -- these tests now pin the advisory's own shape instead.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Semicolon/operator-in-quoted-value false positive (2026-07-26 fix). A
# `;`/`&&`/`||`/`|` INSIDE a quoted argument value (e.g. a cross-repo-memo
# `--title`) is not a real command separator -- the naive
# `re.split(r"[;&|\n]+", cmd_text)` this evaluator used to segment on split
# such a quoted operator anyway, breaking the segment into two bogus halves,
# one an unterminated-quote fragment that still matched the cheap
# `_WRAPPER_PROBE_RE` (via the `python3` interpreter token) and then failed
# `shlex.split`, tripping the fail-closed "unparseable indirection wrapper"
# path on an entirely ordinary quoted value. Reproduced live sending a
# cross-repo memo with a semicolon in `--title`. Fixed by segmenting via the
# same quote-aware `_tokenize_full_command`/`_segments_from_tokens` pair the
# tokenized authoritative pass already uses.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Boundary-anchor widen + tokenized-path fixes (2026-07-21 code review,
# Findings 1-4). Each case reproduced a real bypass on pre-fix disk (verified
# independently against current HEAD before landing, per review-integrator
# discipline) -- a quoted interpreter/verb name, command substitution /
# backtick / subshell-paren wrapping, a bundled `-ic` short flag, and a
# versioned `python3.NN` binary each slipped past Layer 1 with zero
# identity-resolution cost.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# 2026-07-21 security-audit hardening -- deeper bypass class (findings
# #1-#6). Root cause: the git/rm/chmod/chown verb-identification used
# raw-substring regex matching over the command TEXT -- any trick that stops
# the destructive verb's literal characters from appearing CONTIGUOUS
# defeats it. Fixed by tokenizing with `shlex` and matching the resulting
# argv[0] TOKEN, not a raw-string regex substring (`_evaluate_tokenized`).
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Benign-regression coverage for the tokenized authoritative pass -- must
# NOT over-block ordinary subagent Bash.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Heredoc-body false-positive fix (2026-07-23). A heredoc BODY is stdin DATA,
# never shell command tokens -- prose inside a benign file-write heredoc that
# happens to contain the bare word "git"/"rm"/"chmod" must not be classified
# as a destructive command. See `_strip_heredoc_bodies` docstring.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# `<<\EOF`-style backslash-escaped heredoc delimiter (2026-07-29 review
# finding). `<<\EOF` is the standard POSIX spelling for a non-expanding
# heredoc, equivalent in effect to `<<'EOF'` -- `_HEREDOC_OP_RE` previously
# had no branch for the leading `\` and silently failed to match this
# spelling at all, so `_strip_heredoc_bodies` left such a heredoc's body
# untouched and it was rescanned as live command text. Same false-deny class
# `test_heredoc_benign_prose_containing_git_word_allows` pins for `<<'EOF'`,
# just for the untested backslash spelling.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Verb-anchored classification (2026-07-25 false-positive fix). The git
# subcommand classifier used to `\bword\b`-search the whole segment text, so
# a mention of a verb inside a quoted argument, or a hyphenated sibling
# subcommand, was indistinguishable from the verb actually being invoked.
# Two confirmed false positives (both previously DENIED as "git merge (not
# --ff-only)"): `git grep <pattern containing "merge">` and
# `git merge-base --is-ancestor A B`. Fixed by anchoring classification on
# the real argv subcommand (`_git_subcommand_for_segment` /
# `_evaluate_git_segment_anchored`), not a free-text search.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# `--no-optional-locks` enumeration (C2, docs/plans/2026-08-11-pytest-grant-
# and-working-interpreter-disjoint.md). Before this fix, the flag was absent
# from `_GIT_GLOBAL_OPT_NO_ARG`, so the anchored parser bailed on the
# unrecognized flag and handed the segment to the legacy classifier, where
# `_MERGE_WORD_RE` matched inside `merge-base`.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Legacy-path worktree/remote default-deny (2026-07-25 fix, P2(b)). These
# force routing to `_evaluate_git_segment_legacy` via the SAME unenumerated
# `--namespace` global-flag ambiguity used above, so the ANCHORED classifier
# never runs -- exercising the free-text next-word check
# (`_NEXT_WORD_AFTER_RE` gated on `_LEGACY_WORKTREE_READONLY`/
# `_LEGACY_REMOTE_READONLY`) that replaced legacy's former allow-by-default
# on any unenumerated worktree/remote second-level verb.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# P0 security-regression fix (2026-07-25 review): `_real_git_subcommand`
# used to treat ANY unrecognized `--xxx` token as taking NO argument, so an
# unenumerated space-separated global option (`--namespace <path>`,
# `--super-prefix <path>`, `--config-env <name>=<var>`, `--exec-path <path>`)
# caused the flag's own VALUE token to be misresolved as the subcommand,
# leaving the REAL subcommand (and any destructive flags after it, e.g.
# `push --force`) never inspected. Fixed by treating any unrecognized
# `-`/`--`-prefixed token (that isn't a self-contained `--foo=value` form)
# as "argument shape uncertain" and failing the segment over to the legacy
# free-text classifier, which correctly denies these on plain word/flag
# presence regardless of argv position.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# `worktree`/`remote` second-level subcommand classification (2026-07-25
# fix). Both verbs sat in the safe sets (`_SAFE_GIT_SUBCOMMANDS` and the
# legacy `_SAFE_VERB_RE`) as BARE VERBS with no inspection of the
# second-level subcommand, so every mutating form allowed uninspected.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Invocation-head normalization (2026-07-25 fix). `_GIT_SURFACE_RE` and
# `_evaluate_tokenized`'s `norm_head == "git"` check only recognized an
# exact `git` basename, so `git.exe`, a Windows absolute path, and a
# forward-slash-prefixed path all needed checking; the forward-slash forms
# already worked via `.rsplit("/", 1)`, the `.exe`/backslash forms did not.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# P1 adversarial-shape regression tests (2026-07-25 review-integration).
# Locks in behavior empirically confirmed via `guard.check()` -- the module's
# authoritative entry point. `_evaluate_git_segment`/`_git_subcommand_for_segment`
# are NOT valid oracles for these shapes: a synthetic segment fed straight to
# the inner classifier skips Layer 1 surface-gating and identity resolution
# entirely, so it can report a false ALLOW or DENY that `check()` itself does
# not produce (this is exactly how an earlier verification attempt at this
# same finding went wrong before landing on this harness).
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# 2026-07-28 P0 fix -- SPACED-WINDOWS-PATH ARGV0 HOLE. `C:\Program
# Files\Git\bin\git.exe` (git-for-Windows' DEFAULT install location) evaded
# every check above because `_normalize_windows_wrapper_argv0`'s raw-token
# capture (`_RAW_HEAD_TOKEN_RE`) stopped at the first whitespace inside the
# UNQUOTED path, so the path was only ever partially rewritten and
# downstream `shlex.split` either mangled it (backslash form) or split it
# on the embedded space (forward-slash form) -- landing "git"/"bash" one
# argv position too late for this module's position-anchored argv0 checks.
# `_normalize_windows_argv0_head_path_with_spaces` closes this by quoting
# an unquoted, whitespace-containing argv0-head path before `shlex` ever
# runs. Covers both separator forms; DENY-regression coverage for the
# already-quoted/no-space/wrapper-indirection shapes lives in the tests
# above/below and is deliberately not duplicated here.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Spaced UNC path (2026-07-28 review, Finding 4, P2): a UNC path opens with
# TWO leading backslashes, not one -- the original spaced-path regex anchor
# only accepted a single leading separator (or a drive letter + one
# separator), so a spaced UNC path fell through both passes unrewritten and
# still evaded detection. See `_WINDOWS_ARGV0_HEAD_PATH_RE`'s own comment
# for the anchor-widening fix.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Case-variant executable basenames (2026-07-28 review, Finding 1, P1 --
# still-live bypass): Windows PATH/cmd.exe resolution is case-insensitive,
# so `GIT.EXE`, `Git.exe`, `GIT` are real, executable invocations on
# Windows exactly like the lowercase spelling -- every identity check in
# this module must fold case, not just the `.exe`-suffix strip.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# 2026-07-25 P0 fix -- SAFE-FORWARD OPTION-SURFACE HARDENING. The five
# confirmed live exploits from the dispatch brief (`_SAFE_GIT_SUBCOMMANDS`
# and several verb-level allows granted the whole remaining command line
# with no option inspection at all), plus allow-regression coverage proving
# these fixes did NOT over-deny the legitimate forms.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# 2026-07-26 UNSCOPED-STASH GAP CLOSE (module docstring) -- bare `git stash`
# / `git stash push` with no `--`-delimited pathspec sweeps every
# uncommitted change on the shared tree; only a pathspec-scoped push, and
# the read-only list/show forms, may remain allowed.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# SETTINGS-HOME BIN NARROW EXEMPTION (2026-07-27) -- the doctrine-mandated
# settings-home CLI invocation form
# `"${COORDINATOR_SETTINGS_HOME:-$HOME/.coordinator-claude-settings}/bin/<cli>"`
# must ALLOW despite its unresolved-argv0 shape; every bullet the exemption
# regex was narrowed against (see `_SETTINGS_HOME_BIN_EXEMPT_RE`'s module
# comment) must still DENY.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# 2026-07-28 `git mv` CARVE-OUT DECLINED (DoE memo 2026-07-25 P2) -- the
# verdict is unchanged (still denied); what changed is that `mv` is now
# classified by name so the deny message can offer the `mv`-plus-report
# forward path instead of the generic unrecognized-verb text. These tests
# pin BOTH halves: the deny survives, and the offer is present.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# COMMAND-POSITION GIT-TOKEN FIX (2026-07-28) -- confirmed live: a subagent's
# plain `grep -n "git " file | head -50` (the token "git " appearing only
# inside a double-quoted grep PATTERN, never invoked as a command) was denied
# as "unrecognized git verb (default-deny)". Root cause: the anchored
# per-segment classifier searched every shlex token in a segment for one
# equal to `git`, not just the COMMAND-POSITION token; when no exact match
# existed it fell over to the free-text LEGACY classifier, whose own
# default-deny fires on nothing more than the segment containing the
# free-text word "git" (module docstring "COMMAND-POSITION GIT-TOKEN FIX").
#
# Per the class-precedent lesson this fix's commit message cites (a
# deny-guard tested only on the side that denies lets a pattern that merely
# RESEMBLES its target through unnoticed), this section is deliberately
# split into an ALLOW half (the previously-missing coverage) and a DENY half
# (regression pins for every verb this guard's own deny ladder enumerates,
# now exercised through the SAME command-position code path).
# ---------------------------------------------------------------------------


# --- ALLOW side: "git" present as data, never invoked as a command --------


# --- DENY side: every subshell/env/pipe-adjacent shape must still deny ----


# ---------------------------------------------------------------------------
# FAIL-OPEN OBSERVABILITY (2026-07-29 addition) -- pins that each of the
# three fail-open branches now records an identity-resolution tuple to the
# settings-home fail-open log, that a fail-open on a NON-flagged command
# logs nothing (cost-gating preserved), that normal deny/allow behavior is
# byte-for-byte unchanged, and that a broken logging path never raises or
# changes a verdict. Every test here redirects `guard._fail_open_log_path`
# to a tmp file so no test writes into the real settings-home log.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# MACHINE-LOCAL REGISTRY WRITE DENY (2026-08-03, DR-125 implementation --
# code-reviewer Finding 3, narrow-subagent-commit-confinement-two-classes.md
# chunk C2 review). Keyed on COMMAND SHAPE for EVERY resolved subagent type,
# NOT on `_helpers._CONFINED_FINDINGS_AGENTS` membership -- see the module
# comment above `_MACHINE_LOCAL_WRITE_SUBCOMMANDS`. Subcommand list verified
# against the real CLI's `main()` subparser dispatch table
# (`<settings-home>/bin/_machine_local.py`), not guessed.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# POWERSHELL-DIALECT CASES (C4d, 2026-08-07). Every command here is one of
# the twelve measured returning bare `None` in the spike verdict record
# (docs/research/spike-verdicts/2026-08-07-powershell-guard-detection-and-
# tokenizer-mechanism.md, Table 1), plus the one that already denied
# (`rm -Recurse -Force <path>`, kept here as a regression pin on the "do not
# duplicate an alias collision" finding) and the object-pipeline blind spot
# (docs/reference/guard-dialect-coverage.md, "Object-pipeline defeats").
# `requires_powershell_grammar` skips these cleanly on a peer/clean-install
# box that lacks tree-sitter-pwsh (C8's dependency, not yet declared) --
# see that marker's own docstring in test_command_tokenizer_length_ceiling.py.
# ---------------------------------------------------------------------------
import importlib.util


_PS_GRAMMAR_PRESENT = all(
    importlib.util.find_spec(name) is not None
    for name in ("tree_sitter", "tree_sitter_pwsh")
)
requires_powershell_grammar = pytest.mark.skipif(
    not _PS_GRAMMAR_PRESENT,
    reason=(
        "PowerShell grammar package not installed; C8 declares it in "
        "pyproject.toml."
    ),
)


# ---------------------------------------------------------------------------
# FAIL-OPEN LOG: rotation + synthetic-corpus suppression
# ---------------------------------------------------------------------------


_GOOD_PATCH = "diff --git a/pkg/a.py b/pkg/a.py\n--- a/pkg/a.py\n+++ b/pkg/a.py\n@@ -1 +1 @@\n-x\n+y\n"


# ---- process-kill by explicit PID owned by the calling agent ----

_EXEC = "coordinator:executor"


@pytest.fixture(autouse=True)
def _ledger_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the guard's ledger append out of the real settings home."""
    from coordinator_core.bash_guards import agent_process_ownership as apo

    monkeypatch.setattr(apo, "settings_home", lambda: tmp_path / "settings-home")


_URL = "https://github.com/o/r.git"


_COMMIT_V2_ROUTE = (
    '"${COORDINATOR_SETTINGS_HOME:-$HOME/.coordinator-claude-settings}/bin/coordinator-invoke" '
    "ceremony.commit_v2 --repo E:/example-retrieval-repo "
    '\'{"paths":["a.py"],"skip_missing":true,"gone_tracked_as_deleted":true,"message":"wave 1"}\''
)


# --- declared-pathspec revert admission (`git checkout -- <file>` / `git restore <file>`) ---

_REV_RUN = "warp-2026-10-25-demo-20261025T000000-abcdef"
_REV_ROW = "C4"
_REV_SESSION = "sess-emitter"
_REV_MARK = f"COORDINATOR_DISPATCH_ROW={_REV_RUN}/{_REV_ROW}"
_REV_DECLARED = "pkg/a.py"


# --- git-commit-agent: `git -C <repo> commit -- <pathspec>` ---


# ---- private-index plumbing (GIT_INDEX_FILE commit route) ----

_PRIVATE_COMMIT_LINE = (
    "export GIT_INDEX_FILE=scratch/idx-abc987 && git read-tree HEAD && "
    "git apply --cached scratch/x.patch && git write-tree && "
    "git commit-tree abc123 -p HEAD -m msg && "
    "git update-ref refs/heads/work/x deadbeef cafef00d"
)


# ---- git clean: a subagent is denied every form but a dry run ----

_CLEAN_DENIED = ["git clean -f untracked.txt", "git clean -fd", "git clean -fdx", "git clean -xdf", "git clean --force",
                 "git clean -d", "git -C . clean -f untracked.txt"]
_CLEAN_DRY = ["git clean -n", "git clean -nd", "git clean -fdn", "git clean --dry-run", "git clean -f --dry-run"]


_HOST_CLEAN = ("git clean -f untracked.txt", "git clean -fd", "git clean -fdx")


@pytest.fixture
def host_repo(monkeypatch, tmp_path):
    import subprocess

    from coordinator_core.host_hooks import agy_adapter, shim

    monkeypatch.setattr(shim, "_budgeted", lambda request, budget: ({}, False))
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path / "settings-home"))
    monkeypatch.setenv("MACHINE_LOCAL_REGISTRY_DIR", str(tmp_path / "registry"))
    monkeypatch.setenv("COORDINATOR_WARM_RUNTIME_BASE", str(tmp_path / "warm"))
    monkeypatch.delenv("COORDINATOR_SESSION_ID", raising=False)
    monkeypatch.setattr(agy_adapter, "conversations_dir", lambda: tmp_path / "no-agy-store")
    repo = tmp_path / "owned"
    repo.mkdir()
    for args in (("init", "-q", "-b", "main"), ("remote", "add", "origin", "https://github.com/me/thing.git")):
        subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    (repo / "untracked.txt").write_text("u", encoding="utf-8")
    return repo


def _host_denies(host: str, command: str, repo, subagent: bool) -> bool:
    from coordinator_core.host_hooks import shim

    if host == "agy":
        payload = {"conversationId": "1efd6565-4b6a-434a-989d-f7814c28bdd8", "workspacePaths": [str(repo)],
                   "toolCall": {"name": "run_command", "args": {"CommandLine": command}}, "stepIdx": 3}
        if subagent:
            payload["agentId"] = "7c1f0e52-3a4b-4d6e-9f10-2b8a5c3d4e60"
        return shim.decide("agy", payload).get("decision") == "deny"
    payload = {"hook_event_name": "PreToolUse", "session_id": "clean-s1", "cwd": str(repo),
               "tool_name": "exec_command" if host == "codex" else "run_command", "tool_input": {"command": command}}
    if subagent:
        payload.update({"agent_id": "01a1ec94-2eed-7c02-8753-e7f1d8e8ed56", "agent_type": "worker"})
    answer = shim.decide(host, payload)
    return (answer.get("hookSpecificOutput") or {}).get("permissionDecision") == "deny"


@pytest.mark.spawns_process
@pytest.mark.parametrize("host", ["codex", "agy", "grok"])
def test_host_shapes_deny_a_subagent_clean_and_allow_dry_run_and_em(host_repo, host) -> None:
    for command in _HOST_CLEAN:
        assert _host_denies(host, command, host_repo, subagent=True), (host, command)
        assert not _host_denies(host, command, host_repo, subagent=False), (host, command)
    assert not _host_denies(host, "git clean -n", host_repo, subagent=True)
    assert not _host_denies(host, "git clean --dry-run", host_repo, subagent=True)
