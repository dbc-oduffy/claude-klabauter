#!/usr/bin/env bash
# bin/tests/test-check-plugin-drift-copy-install.sh
#
# Purpose: Unit tests for the copy_install detection branch in check-plugin-drift.py.
#
# Spec backlink: docs/plans/2026-05-23-copy-install-drift-coverage.md § Chunk 3 (AC-6)
#
# All git/dir construction is done in tmp — never touches the real ~/.claude/plugins.
# The test builds synthetic source git repos and synthetic live dirs to cover the
# full AC-2 / AC-3 matrix without network or live-install side effects.
#
# Run: bash plugins/coordinator/bin/tests/test-check-plugin-drift-copy-install.sh

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROBE="${SCRIPT_DIR}/../check-plugin-drift.py"
REFRESH="${SCRIPT_DIR}/../refresh-plugin-live-install.py"

# ---------------------------------------------------------------------------
# CLAUDE_KLABAUTER_ROOT preflight (2026-07-22 shell-suite liveness sweep).
#
# check-plugin-drift.py is now a trampoline over claude-klabauter's
# coordinator_core.plugin_health.drift, resolved via cc_invoke's ladder:
# CLAUDE_KLABAUTER_ROOT env var → <settings-home>/machine-local/.claude-klabauter-root pointer
# (under $HOME) → registry lookup. Every probe invocation below stubs
# HOME="$TMP_ROOT" (deliberately — it keeps git hash-object context and any
# live-install lookups inside tmp), which severs every HOME-anchored rung of
# that ladder. So: resolve CLAUDE_KLABAUTER_ROOT ONCE here with the real HOME and export
# it — rung 1 (env var) then survives the per-invocation HOME stub. If the
# root is genuinely unresolvable on this machine (no claude-klabauter checkout
# registered), graceful-skip with exit 0 — transport-fail skip parity with
# coordinator/tests/test_coordinator_session.py; a missing sibling checkout is
# an environment gap, not a drift-probe regression.
# ---------------------------------------------------------------------------
if [[ -z "${CLAUDE_KLABAUTER_ROOT:-}" ]]; then
    CLAUDE_KLABAUTER_ROOT="$(python3 - "$SCRIPT_DIR" <<'PY' 2>/dev/null
import sys
sys.path.insert(0, sys.argv[1] + "/../lib")
from cc_invoke import _resolve_claude_klabauter_root
print(_resolve_claude_klabauter_root())
PY
)" || CLAUDE_KLABAUTER_ROOT=""
fi
if [[ -z "${CLAUDE_KLABAUTER_ROOT}" ]] || [[ ! -d "${CLAUDE_KLABAUTER_ROOT}" ]]; then
    echo "SKIP: test-check-plugin-drift-copy-install.sh — CLAUDE_KLABAUTER_ROOT unresolvable on this machine (no claude-klabauter checkout registered); drift trampoline has no engine to import. Graceful skip, exit 0."
    exit 0
fi
export CLAUDE_KLABAUTER_ROOT

# ---------------------------------------------------------------------------
# Test framework
# ---------------------------------------------------------------------------

PASS=0
FAIL=0
FAIL_MSGS=()

pass() { echo "  PASS: $1"; (( PASS++ )) || true; }
fail() {
    echo "  FAIL: $1"
    FAIL_MSGS+=("$1")
    (( FAIL++ )) || true
}

run_test() {
    local name="$1"
    local fn="$2"
    echo "--- $name"
    if "$fn"; then
        pass "$name"
    else
        fail "$name"
    fi
}

assert_eq() {
    local label="$1" expected="$2" actual="$3"
    if [[ "$expected" == "$actual" ]]; then
        return 0
    else
        echo "    Expected: $(printf '%q' "$expected")" >&2
        echo "    Actual:   $(printf '%q' "$actual")" >&2
        return 1
    fi
}

assert_contains() {
    local label="$1" needle="$2" haystack="$3"
    if printf '%s\n' "$haystack" | grep -qF "$needle"; then
        return 0
    else
        echo "    Expected to contain: ${needle}" >&2
        echo "    In: ${haystack}" >&2
        return 1
    fi
}

assert_exit() {
    local label="$1" expected_exit="$2" actual_exit="$3"
    if [[ "$expected_exit" == "$actual_exit" ]]; then
        return 0
    else
        echo "    Expected exit: $expected_exit" >&2
        echo "    Actual exit:   $actual_exit" >&2
        return 1
    fi
}

# ---------------------------------------------------------------------------
# Scratch infrastructure
# ---------------------------------------------------------------------------

TMP_ROOT=""

setup_tmp() {
    TMP_ROOT="$(mktemp -d)"
}

teardown_tmp() {
    if [[ -n "$TMP_ROOT" ]] && [[ -d "$TMP_ROOT" ]]; then
        rm -rf "$TMP_ROOT"
    fi
}

# Create a synthetic source git repo and return its path via stdout (capture at call site).
# Review: code-reviewer (F9) — removed unused out_var param and misleading nameref comment;
# function just echoes the path; callers do: source_dir="$(make_source_repo)".
make_source_repo() {
    local repo_dir="${TMP_ROOT}/source-${RANDOM}"
    mkdir -p "$repo_dir"
    git -C "$repo_dir" init -q
    git -C "$repo_dir" config user.email "test@test.invalid"
    git -C "$repo_dir" config user.name "Test"
    echo "source content" > "$repo_dir/README.md"
    git -C "$repo_dir" add README.md
    git -C "$repo_dir" commit -q -m "initial"
    echo "$repo_dir"
}

# Return the HEAD SHA of a repo.
head_sha() {
    git -C "$1" rev-parse HEAD 2>/dev/null | tr -d '\r'
}

# Create a synthetic live dir with an optional version.txt content.
make_live_dir() {
    local sentinel_content="${1:-}"   # empty = no version.txt
    local live_dir="${TMP_ROOT}/live-${RANDOM}"
    mkdir -p "$live_dir"
    if [[ -n "$sentinel_content" ]]; then
        printf '%s' "$sentinel_content" > "$live_dir/version.txt"
    fi
    echo "$live_dir"
}

# Write a minimal TOML registry with a single copy_install entry and return the path.
# Review: code-reviewer (F10) — source_subpath absent → probe defaults to plugin/<plugin_name>.
make_registry() {
    local source_path="$1"
    local live_path="$2"
    local registry="${TMP_ROOT}/registry-${RANDOM}.toml"
    cat > "$registry" <<TOML
[plugin.mirrors.test-plugin]
propagation_mode = "copy_install"
source_path = "${source_path}"
live_path   = "${live_path}"
TOML
    echo "$registry"
}

# Write a TOML registry with an explicit source_subpath field.
make_registry_with_subpath() {
    local source_path="$1"
    local live_path="$2"
    local subpath="$3"
    local registry="${TMP_ROOT}/registry-${RANDOM}.toml"
    cat > "$registry" <<TOML
[plugin.mirrors.test-plugin]
propagation_mode = "copy_install"
source_path = "${source_path}"
live_path   = "${live_path}"
source_subpath = "${subpath}"
TOML
    echo "$registry"
}

# Initialize a bare git repo at ${TMP_ROOT}/.claude so CLAUDE_HOME is a valid git context
# for git hash-object calls in the content-equivalence probe code.
setup_claude_home_git() {
    local claude_home="${TMP_ROOT}/.claude"
    mkdir -p "$claude_home"
    git -C "$claude_home" init -q
    git -C "$claude_home" config user.email "test@test.invalid"
    git -C "$claude_home" config user.name "Test"
    # Review: code-reviewer (F7) — set autocrlf true so the CRLF-divergent test (case 8)
    # deterministically exercises the normalization path on all platforms.
    git -C "$claude_home" config core.autocrlf true
    # Commit a placeholder so HEAD exists (hash-object does not require a commit,
    # but some git versions warn on empty repos).
    echo "placeholder" > "$claude_home/.gitkeep"
    git -C "$claude_home" add .gitkeep
    git -C "$claude_home" commit -q -m "placeholder"
}

# ---------------------------------------------------------------------------
# Rename the registry to what the probe expects (registry.local.toml in REGISTRY_DIR).
# ---------------------------------------------------------------------------

install_registry() {
    local src_registry="$1"
    local reg_dir="${TMP_ROOT}/regdir-${RANDOM}"
    mkdir -p "$reg_dir"
    cp "$src_registry" "$reg_dir/registry.local.toml"
    echo "$reg_dir"
}

# ---------------------------------------------------------------------------
# Test cases
# ---------------------------------------------------------------------------

# AC-2(d): sentinel present, matches source HEAD → [ok], exit 0
test_clean_equal_sha() {
    setup_tmp
    local source_dir live_dir reg_dir
    source_dir="$(make_source_repo)"
    local sha; sha="$(head_sha "$source_dir")"
    live_dir="$(make_live_dir "$sha")"
    local registry; registry="$(make_registry "$source_dir" "$live_dir")"
    reg_dir="$(install_registry "$registry")"

    local output exit_code
    output="$(MACHINE_LOCAL_REGISTRY_DIR="$reg_dir" HOME="$TMP_ROOT" python "$PROBE" 2>&1)"
    exit_code=$?

    assert_contains "clean: [ok] in output" "[ok]" "$output" || { teardown_tmp; return 1; }
    assert_exit "clean: exit 0" 0 "$exit_code" || { teardown_tmp; return 1; }
    teardown_tmp
}

# AC-2(e): sentinel present, well-formed, but != source HEAD AND content differs → [drift], exit 1
# Review: code-reviewer (F8) — exercises the content-differs-AND-sentinel-lags path (post-F2
# drift requires BOTH conditions). Distinct from the content-equivalent case (test case 1 in
# the content-equivalence section) where sentinel lags but live content already matches HEAD.
# Fixture note: files must live under plugin/test-plugin/ so git ls-tree on source_subpath
# (defaulting to "plugin/test-plugin") returns them.  The live dir holds old-SHA content so
# blob SHAs diverge, triggering the [drift] path introduced by the content-equivalence fallback.
test_drifted_unequal_sha() {
    setup_tmp
    local source_dir live_dir reg_dir
    source_dir="$(make_source_repo)"

    # Lay initial content under plugin/test-plugin/ (content-equivalence subpath).
    mkdir -p "$source_dir/plugin/test-plugin"
    echo "original content" > "$source_dir/plugin/test-plugin/file.txt"
    git -C "$source_dir" add "plugin/test-plugin/file.txt"
    git -C "$source_dir" commit -q -m "initial plugin content"

    # Record old SHA before advancing source.
    local old_sha; old_sha="$(head_sha "$source_dir")"

    # Advance source to a new commit (different content).
    echo "updated content" > "$source_dir/plugin/test-plugin/file.txt"
    git -C "$source_dir" add "plugin/test-plugin/file.txt"
    git -C "$source_dir" commit -q -m "update plugin content"

    # Live dir: sentinel at old SHA; content also old (SHA mismatch → drift).
    live_dir="$(make_live_dir "$old_sha")"
    echo "original content" > "$live_dir/file.txt"

    local registry; registry="$(make_registry "$source_dir" "$live_dir")"
    reg_dir="$(install_registry "$registry")"

    local output exit_code
    output="$(MACHINE_LOCAL_REGISTRY_DIR="$reg_dir" HOME="$TMP_ROOT" python "$PROBE" 2>&1)"
    exit_code=$?

    assert_contains "drifted: [drift] in output" "[drift]" "$output" || { teardown_tmp; return 1; }
    assert_contains "drifted: copy_install in output" "copy_install" "$output" || { teardown_tmp; return 1; }
    assert_exit "drifted: exit 1" 1 "$exit_code" || { teardown_tmp; return 1; }
    teardown_tmp
}

# AC-2(b): no version.txt → [info], exit 0 (not drift)
test_no_sentinel_info_exit0() {
    setup_tmp
    local source_dir live_dir reg_dir
    source_dir="$(make_source_repo)"
    live_dir="$(make_live_dir "")"   # no sentinel
    local registry; registry="$(make_registry "$source_dir" "$live_dir")"
    reg_dir="$(install_registry "$registry")"

    local output exit_code
    output="$(MACHINE_LOCAL_REGISTRY_DIR="$reg_dir" HOME="$TMP_ROOT" python "$PROBE" 2>&1)"
    exit_code=$?

    assert_contains "no-sentinel: [info] in output" "[info]" "$output" || { teardown_tmp; return 1; }
    assert_exit "no-sentinel: exit 0" 0 "$exit_code" || { teardown_tmp; return 1; }
    teardown_tmp
}

# AC-2(a): live_path missing → [drift], exit 1
test_missing_live_path() {
    setup_tmp
    local source_dir reg_dir
    source_dir="$(make_source_repo)"
    local nonexistent_live="${TMP_ROOT}/does-not-exist"
    local registry; registry="$(make_registry "$source_dir" "$nonexistent_live")"
    reg_dir="$(install_registry "$registry")"

    local output exit_code
    output="$(MACHINE_LOCAL_REGISTRY_DIR="$reg_dir" HOME="$TMP_ROOT" python "$PROBE" 2>&1)"
    exit_code=$?

    assert_contains "missing-live: [drift] in output" "[drift]" "$output" || { teardown_tmp; return 1; }
    assert_exit "missing-live: exit 1" 1 "$exit_code" || { teardown_tmp; return 1; }
    teardown_tmp
}

# AC-3: --check-clean-only on copy_install → exit 0 (no live git working tree)
test_check_clean_only_exit0() {
    setup_tmp
    local source_dir live_dir reg_dir
    source_dir="$(make_source_repo)"
    local sha; sha="$(head_sha "$source_dir")"
    live_dir="$(make_live_dir "$sha")"
    local registry; registry="$(make_registry "$source_dir" "$live_dir")"
    reg_dir="$(install_registry "$registry")"

    local output exit_code
    output="$(MACHINE_LOCAL_REGISTRY_DIR="$reg_dir" HOME="$TMP_ROOT" python "$PROBE" --check-clean-only 2>&1)"
    exit_code=$?

    assert_exit "--check-clean-only: exit 0" 0 "$exit_code" || { teardown_tmp; return 1; }
    teardown_tmp
}

# AC-2(c) / AC-6 malformed sentinel: non-40-hex-char string → [warn], exit 0 (not drift)
test_malformed_sentinel_warn_not_drift() {
    setup_tmp
    local source_dir live_dir reg_dir
    source_dir="$(make_source_repo)"
    # Write a sentinel that is clearly malformed (wrong length, non-hex chars).
    live_dir="$(make_live_dir "not-a-sha")"
    local registry; registry="$(make_registry "$source_dir" "$live_dir")"
    reg_dir="$(install_registry "$registry")"

    local output exit_code
    output="$(MACHINE_LOCAL_REGISTRY_DIR="$reg_dir" HOME="$TMP_ROOT" python "$PROBE" 2>&1)"
    exit_code=$?

    assert_contains "malformed: [warn] in output" "[warn]" "$output" || { teardown_tmp; return 1; }
    assert_contains "malformed: 'malformed' in output" "malformed" "$output" || { teardown_tmp; return 1; }
    assert_exit "malformed: exit 0 (not drift)" 0 "$exit_code" || { teardown_tmp; return 1; }
    teardown_tmp
}

# AC-6 restore-orphan: REPLACE-vs-overlay restore semantics in the copy_install
# restore path of refresh-plugin-live-install.py.
#
# Two-part guard (code-reviewer F5: a POSIX `rm-rf + cp-r` demo alone tests its own
# implementation, not the script). Part A is the behavioral demonstration; Part B is
# the actual regression guard — a structural assertion that the copy_install handler
# calls the dedicated `_replace_restore()` helper (stage-then-swap: full live-path
# wipe before restoring the snapshot) and NEVER the `_overlay_restore()` helper the
# git-checkout-managed (default) restore path uses (merge-only, extraneous live files
# survive — correct there because live IS a git checkout, wrong for copy_install where
# a failed install may have left orphans). The live end-to-end restore path is
# additionally proven by the AC-9 dogfood (refresh-plugin-live-install.py
# example-game-repo-control) recorded in the plan.
test_restore_orphan_gone() {
    setup_tmp

    # Part A — behavioral demonstration of REPLACE semantics.
    local snapshot_dir="${TMP_ROOT}/snapshot"
    mkdir -p "$snapshot_dir"
    echo "original_file" > "$snapshot_dir/original.txt"
    local live_dir="${TMP_ROOT}/live"
    cp -r "$snapshot_dir" "$live_dir"
    echo "orphan" > "$live_dir/orphan_from_failed_install.txt"
    rm -rf "$live_dir"
    cp -r "$snapshot_dir" "$live_dir"
    if [[ -f "$live_dir/orphan_from_failed_install.txt" ]]; then
        echo "    FAIL: orphan survived REPLACE restore (Part A)" >&2
        teardown_tmp; return 1
    fi
    if [[ ! -f "$live_dir/original.txt" ]]; then
        echo "    FAIL: original missing after restore (Part A)" >&2
        teardown_tmp; return 1
    fi

    # Part B — structural regression guard against the COPY_INSTALL handler reverting
    # to overlay. Scoped to the `_handle_copy_install` function body only (the
    # pre-existing git-managed restore legitimately uses `_overlay_restore()` because
    # its live IS a git checkout — a global grep would false-positive on it).
    local ci_region
    ci_region="$(awk '/^def _handle_copy_install\(/,/^def _handle_editable_sibling_venv\(/' "$REFRESH")"
    if [[ -z "$ci_region" ]]; then
        echo "    FAIL: could not locate _handle_copy_install(...) function body in $REFRESH" >&2
        teardown_tmp; return 1
    fi
    if ! printf '%s\n' "$ci_region" | grep -qF '_replace_restore('; then
        echo "    FAIL: _handle_copy_install no longer calls _replace_restore() (REPLACE semantics lost)" >&2
        teardown_tmp; return 1
    fi
    if printf '%s\n' "$ci_region" | grep -qF '_overlay_restore('; then
        echo "    FAIL: _handle_copy_install calls _overlay_restore() — orphans would survive a failed install" >&2
        teardown_tmp; return 1
    fi

    # _replace_restore() itself must wipe live_path before restoring (not merge into it).
    local replace_fn_region
    replace_fn_region="$(awk '/^def _replace_restore\(/,/^def _acquire_lock\(/' "$REFRESH")"
    if ! printf '%s\n' "$replace_fn_region" | grep -qF '_safe_rmtree(live_path)'; then
        echo "    FAIL: _replace_restore() no longer wipes live_path before swapping in the snapshot" >&2
        teardown_tmp; return 1
    fi

    teardown_tmp
}

# ---------------------------------------------------------------------------
# New test cases — content-equivalence fallback (blob-SHA mechanism)
# Spec backlink: docs/plans/2026-05-28-forward-drift-probe-content-equivalence.md §Chunk2
# ---------------------------------------------------------------------------

# Helper: make a source repo with tracked content under plugin/test-plugin/.
# Returns path to source repo; SHA of "B" commit via the second argument (by echoing).
# Usage: source_dir="$(make_source_repo_with_plugin_content)" ; b_sha="$(head_sha "$source_dir")"
make_source_repo_for_content_equiv() {
    local subpath="${1:-plugin/test-plugin}"
    local repo_dir="${TMP_ROOT}/source-${RANDOM}"
    mkdir -p "$repo_dir/$subpath"
    git -C "$repo_dir" init -q
    git -C "$repo_dir" config user.email "test@test.invalid"
    git -C "$repo_dir" config user.name "Test"
    # Commit A: initial content
    echo "initial" > "$repo_dir/$subpath/file.txt"
    git -C "$repo_dir" add "$subpath/file.txt"
    git -C "$repo_dir" commit -q -m "commit A"
    # Commit B: updated content (this will be the HEAD the sentinel lags behind)
    echo "updated" > "$repo_dir/$subpath/file.txt"
    git -C "$repo_dir" add "$subpath/file.txt"
    git -C "$repo_dir" commit -q -m "commit B"
    echo "$repo_dir"
}

# 1. sentinel-lagging-content-equivalent
# Source at HEAD B; live tree has blob SHAs matching B; sentinel reads SHA A.
# Probe must exit 0 with [ok-via-git-propagation], both SHA prefixes, no [drift].
test_sentinel_lagging_content_equivalent() {
    setup_tmp
    setup_claude_home_git

    local source_dir live_dir reg_dir
    source_dir="$(make_source_repo_for_content_equiv "plugin/test-plugin")"
    local b_sha; b_sha="$(head_sha "$source_dir")"
    # Get SHA A (parent of B)
    local a_sha; a_sha="$(git -C "$source_dir" rev-parse HEAD~1 2>/dev/null | tr -d '\r')"

    # Live dir: content matches B (same bytes as source B), sentinel at A.
    live_dir="$(make_live_dir "$a_sha")"
    # Copy B content to live dir (verbatim — no installer transformation).
    echo "updated" > "$live_dir/file.txt"

    local registry; registry="$(make_registry "$source_dir" "$live_dir")"
    reg_dir="$(install_registry "$registry")"

    local output exit_code
    output="$(MACHINE_LOCAL_REGISTRY_DIR="$reg_dir" HOME="$TMP_ROOT" python "$PROBE" 2>&1)"
    exit_code=$?

    assert_contains "content-equiv: [ok-via-git-propagation]" "[ok-via-git-propagation]" "$output" || { teardown_tmp; return 1; }
    assert_contains "content-equiv: source HEAD prefix" "${b_sha:0:12}" "$output" || { teardown_tmp; return 1; }
    assert_contains "content-equiv: sentinel SHA prefix" "${a_sha:0:12}" "$output" || { teardown_tmp; return 1; }
    if printf '%s\n' "$output" | grep -qF "[drift]"; then
        echo "    [drift] must NOT appear in content-equiv output" >&2; teardown_tmp; return 1
    fi
    assert_exit "content-equiv: exit 0" 0 "$exit_code" || { teardown_tmp; return 1; }
    teardown_tmp
}

# 2. sentinel-lagging-content-differs
# Source at B; live has old-content file (SHA mismatch); sentinel at A.
# Probe must exit 1 with [drift] mentioning the differing file.
test_sentinel_lagging_content_differs() {
    setup_tmp
    setup_claude_home_git

    local source_dir live_dir reg_dir
    source_dir="$(make_source_repo_for_content_equiv "plugin/test-plugin")"
    local a_sha; a_sha="$(git -C "$source_dir" rev-parse HEAD~1 2>/dev/null | tr -d '\r')"

    # Live dir: OLD content (sentinel at A, content also at A — genuinely stale).
    live_dir="$(make_live_dir "$a_sha")"
    echo "initial" > "$live_dir/file.txt"   # old content, not matching source B

    local registry; registry="$(make_registry "$source_dir" "$live_dir")"
    reg_dir="$(install_registry "$registry")"

    local output exit_code
    output="$(MACHINE_LOCAL_REGISTRY_DIR="$reg_dir" HOME="$TMP_ROOT" python "$PROBE" 2>&1)"
    exit_code=$?

    assert_contains "content-differs: [drift]" "[drift]" "$output" || { teardown_tmp; return 1; }
    assert_contains "content-differs: file.txt in summary" "file.txt" "$output" || { teardown_tmp; return 1; }
    assert_exit "content-differs: exit 1" 1 "$exit_code" || { teardown_tmp; return 1; }
    teardown_tmp
}

# 3. sentinel-lagging-content-differs-many-files (15 files, cap at 10 + "… + 5 more")
test_sentinel_lagging_content_differs_many_files() {
    setup_tmp
    setup_claude_home_git

    local source_dir live_dir reg_dir
    local subpath="plugin/test-plugin"
    source_dir="${TMP_ROOT}/source-${RANDOM}"
    mkdir -p "$source_dir/$subpath"
    git -C "$source_dir" init -q
    git -C "$source_dir" config user.email "test@test.invalid"
    git -C "$source_dir" config user.name "Test"

    # Commit A: 15 files
    for i in $(seq 1 15); do
        echo "old-content-${i}" > "$source_dir/$subpath/file${i}.txt"
    done
    git -C "$source_dir" add "$subpath/"
    git -C "$source_dir" commit -q -m "commit A"
    local a_sha; a_sha="$(head_sha "$source_dir")"

    # Commit B: update all 15 files
    for i in $(seq 1 15); do
        echo "new-content-${i}" > "$source_dir/$subpath/file${i}.txt"
    done
    git -C "$source_dir" add "$subpath/"
    git -C "$source_dir" commit -q -m "commit B"

    # Live dir: old content for all 15 files, sentinel at A
    live_dir="$(make_live_dir "$a_sha")"
    for i in $(seq 1 15); do
        echo "old-content-${i}" > "$live_dir/file${i}.txt"
    done

    local registry; registry="$(make_registry "$source_dir" "$live_dir")"
    reg_dir="$(install_registry "$registry")"

    local output exit_code
    output="$(MACHINE_LOCAL_REGISTRY_DIR="$reg_dir" HOME="$TMP_ROOT" python "$PROBE" 2>&1)"
    exit_code=$?

    assert_contains "many-files: [drift]" "[drift]" "$output" || { teardown_tmp; return 1; }
    assert_contains "many-files: truncation line" "… + 5 more" "$output" || { teardown_tmp; return 1; }
    assert_exit "many-files: exit 1" 1 "$exit_code" || { teardown_tmp; return 1; }
    teardown_tmp
}

# 4. sentinel-lagging-source-subpath-missing
# source_subpath does not exist on disk; sentinel lags.
# Probe must exit 0 with [warn], NOT [drift] or [ok-via-git-propagation].
test_sentinel_lagging_source_subpath_missing() {
    setup_tmp
    setup_claude_home_git

    local source_dir live_dir reg_dir
    source_dir="$(make_source_repo)"
    local sha; sha="$(head_sha "$source_dir")"
    # Advance source to create lagging sentinel.
    echo "second" > "$source_dir/second.md"
    git -C "$source_dir" add second.md
    git -C "$source_dir" commit -q -m "second"
    # Sentinel at old SHA; source_subpath will default to plugin/test-plugin which doesn't exist.
    live_dir="$(make_live_dir "$sha")"

    local registry; registry="$(make_registry "$source_dir" "$live_dir")"
    reg_dir="$(install_registry "$registry")"

    local output exit_code
    output="$(MACHINE_LOCAL_REGISTRY_DIR="$reg_dir" HOME="$TMP_ROOT" python "$PROBE" 2>&1)"
    exit_code=$?

    assert_contains "subpath-missing: [warn]" "[warn]" "$output" || { teardown_tmp; return 1; }
    if printf '%s\n' "$output" | grep -qF "[drift]"; then
        echo "    [drift] must NOT appear when source_subpath is missing" >&2; teardown_tmp; return 1
    fi
    if printf '%s\n' "$output" | grep -qF "[ok-via-git-propagation]"; then
        echo "    [ok-via-git-propagation] must NOT appear when source_subpath is missing" >&2; teardown_tmp; return 1
    fi
    assert_exit "subpath-missing: exit 0" 0 "$exit_code" || { teardown_tmp; return 1; }
    teardown_tmp
}

# 5. sentinel-lagging-git-ls-tree-fails
# Source path exists but has no .git/ (not a valid git repo). Sentinel lags.
# Probe must exit 0 with [warn], must NOT emit [drift].
test_sentinel_lagging_git_ls_tree_fails() {
    setup_tmp
    setup_claude_home_git

    # Build a valid source repo to get a real SHA, then strip the .git dir.
    local source_dir live_dir reg_dir
    source_dir="${TMP_ROOT}/source-${RANDOM}"
    mkdir -p "$source_dir/plugin/test-plugin"
    git -C "$source_dir" init -q
    git -C "$source_dir" config user.email "test@test.invalid"
    git -C "$source_dir" config user.name "Test"
    echo "content" > "$source_dir/plugin/test-plugin/file.txt"
    git -C "$source_dir" add "plugin/test-plugin/file.txt"
    git -C "$source_dir" commit -q -m "init"
    local old_sha; old_sha="$(head_sha "$source_dir")"
    echo "v2" > "$source_dir/plugin/test-plugin/file.txt"
    git -C "$source_dir" add "plugin/test-plugin/file.txt"
    git -C "$source_dir" commit -q -m "v2"
    # Now remove .git so git operations fail
    rm -rf "$source_dir/.git"

    live_dir="$(make_live_dir "$old_sha")"
    echo "content" > "$live_dir/file.txt"

    local registry; registry="$(make_registry "$source_dir" "$live_dir")"
    reg_dir="$(install_registry "$registry")"

    local output exit_code
    output="$(MACHINE_LOCAL_REGISTRY_DIR="$reg_dir" HOME="$TMP_ROOT" python "$PROBE" 2>&1)"
    exit_code=$?

    assert_contains "git-ls-tree-fails: [warn]" "[warn]" "$output" || { teardown_tmp; return 1; }
    if printf '%s\n' "$output" | grep -qF "[drift]"; then
        echo "    [drift] must NOT appear when git ls-tree fails" >&2; teardown_tmp; return 1
    fi
    assert_exit "git-ls-tree-fails: exit 0" 0 "$exit_code" || { teardown_tmp; return 1; }
    teardown_tmp
}

# 6. registry-source_subpath-explicit
# Registry sets source_subpath = "subtree/plugin"; probe uses it (not the default).
test_registry_source_subpath_explicit() {
    setup_tmp
    setup_claude_home_git

    local subpath="subtree/plugin"
    local source_dir live_dir reg_dir
    source_dir="${TMP_ROOT}/source-${RANDOM}"
    mkdir -p "$source_dir/$subpath"
    git -C "$source_dir" init -q
    git -C "$source_dir" config user.email "test@test.invalid"
    git -C "$source_dir" config user.name "Test"
    echo "init" > "$source_dir/$subpath/file.txt"
    git -C "$source_dir" add "$subpath/file.txt"
    git -C "$source_dir" commit -q -m "A"
    local a_sha; a_sha="$(head_sha "$source_dir")"
    echo "v2" > "$source_dir/$subpath/file.txt"
    git -C "$source_dir" add "$subpath/file.txt"
    git -C "$source_dir" commit -q -m "B"
    local b_sha; b_sha="$(head_sha "$source_dir")"

    # Live dir: content matches B, sentinel at A.
    live_dir="$(make_live_dir "$a_sha")"
    echo "v2" > "$live_dir/file.txt"

    # Registry with explicit source_subpath (non-default).
    local registry; registry="$(make_registry_with_subpath "$source_dir" "$live_dir" "$subpath")"
    reg_dir="$(install_registry "$registry")"

    local output exit_code
    output="$(MACHINE_LOCAL_REGISTRY_DIR="$reg_dir" HOME="$TMP_ROOT" python "$PROBE" 2>&1)"
    exit_code=$?

    assert_contains "explicit-subpath: [ok-via-git-propagation]" "[ok-via-git-propagation]" "$output" || { teardown_tmp; return 1; }
    assert_exit "explicit-subpath: exit 0" 0 "$exit_code" || { teardown_tmp; return 1; }
    teardown_tmp
}

# 7. registry-source_subpath-default
# Registry omits source_subpath; probe defaults to plugin/test-plugin.
# (Content matches HEAD B → [ok-via-git-propagation])
test_registry_source_subpath_default() {
    setup_tmp
    setup_claude_home_git

    local source_dir live_dir reg_dir
    source_dir="$(make_source_repo_for_content_equiv "plugin/test-plugin")"
    local b_sha; b_sha="$(head_sha "$source_dir")"
    local a_sha; a_sha="$(git -C "$source_dir" rev-parse HEAD~1 2>/dev/null | tr -d '\r')"

    live_dir="$(make_live_dir "$a_sha")"
    echo "updated" > "$live_dir/file.txt"   # matches source B content

    # Plain registry with NO source_subpath field.
    local registry; registry="$(make_registry "$source_dir" "$live_dir")"
    reg_dir="$(install_registry "$registry")"

    local output exit_code
    output="$(MACHINE_LOCAL_REGISTRY_DIR="$reg_dir" HOME="$TMP_ROOT" python "$PROBE" 2>&1)"
    exit_code=$?

    assert_contains "default-subpath: [ok-via-git-propagation]" "[ok-via-git-propagation]" "$output" || { teardown_tmp; return 1; }
    assert_exit "default-subpath: exit 0" 0 "$exit_code" || { teardown_tmp; return 1; }
    teardown_tmp
}

# 8. crlf-divergent-but-content-equal
# Source repo stores LF content. Live copy is written INDEPENDENTLY with CRLF.
# git hash-object in CLAUDE_HOME normalizes via the live repo's clean filter (autocrlf).
# On a system where autocrlf is true, both SHAs normalize to LF → match → [ok-via-git-propagation].
# On a system where autocrlf is false/input, the live CRLF file hashes differently — this
# test exercises the byte path; on such systems [drift] is the correct honest result.
# We assert exit 0 only if the SHAs actually agree (i.e. normalization happened).
# This test never false-fails: it introspects the actual hash-object result.
test_crlf_divergent_but_content_equal() {
    setup_tmp
    setup_claude_home_git

    local source_dir live_dir reg_dir
    source_dir="$(make_source_repo_for_content_equiv "plugin/test-plugin")"
    local b_sha; b_sha="$(head_sha "$source_dir")"
    local a_sha; a_sha="$(git -C "$source_dir" rev-parse HEAD~1 2>/dev/null | tr -d '\r')"

    # Write live copy INDEPENDENTLY with CRLF line endings (different autocrlf context).
    live_dir="$(make_live_dir "$a_sha")"
    printf "updated\r\n" > "$live_dir/file.txt"   # CRLF, written without going through git

    local registry; registry="$(make_registry "$source_dir" "$live_dir")"
    reg_dir="$(install_registry "$registry")"

    # Pre-compute what git hash-object returns for the live file in CLAUDE_HOME context.
    local live_hash
    live_hash="$(git -C "${TMP_ROOT}/.claude" hash-object "$live_dir/file.txt" 2>/dev/null | tr -d '\r')" || live_hash=""

    # Get source blob SHA for file.txt at HEAD B.
    local src_blob
    src_blob="$(git -C "$source_dir" ls-tree HEAD -- "plugin/test-plugin/file.txt" 2>/dev/null | awk '{print $3}' | tr -d '\r')" || src_blob=""

    local output exit_code
    output="$(MACHINE_LOCAL_REGISTRY_DIR="$reg_dir" HOME="$TMP_ROOT" python "$PROBE" 2>&1)"
    exit_code=$?

    if [[ -n "$live_hash" ]] && [[ -n "$src_blob" ]] && [[ "$live_hash" == "$src_blob" ]]; then
        # Normalization happened — SHAs agree → expect [ok-via-git-propagation].
        assert_contains "crlf-equiv: [ok-via-git-propagation]" "[ok-via-git-propagation]" "$output" || { teardown_tmp; return 1; }
        assert_exit "crlf-equiv: exit 0" 0 "$exit_code" || { teardown_tmp; return 1; }
    else
        # SHAs differ (no autocrlf normalization on this system) → [drift] is correct.
        assert_contains "crlf-no-norm: [drift]" "[drift]" "$output" || { teardown_tmp; return 1; }
        assert_exit "crlf-no-norm: exit 1" 1 "$exit_code" || { teardown_tmp; return 1; }
    fi
    teardown_tmp
}

# 9. untracked-artifacts-present
# Live tree has __pycache__/, foo.pyc, and other untracked files alongside tracked
# content that matches source HEAD. Untracked files must not cause false drift.
test_untracked_artifacts_present() {
    setup_tmp
    setup_claude_home_git

    local source_dir live_dir reg_dir
    source_dir="$(make_source_repo_for_content_equiv "plugin/test-plugin")"
    local b_sha; b_sha="$(head_sha "$source_dir")"
    local a_sha; a_sha="$(git -C "$source_dir" rev-parse HEAD~1 2>/dev/null | tr -d '\r')"

    # Live dir: content matches B, sentinel at A, PLUS untracked artifacts.
    live_dir="$(make_live_dir "$a_sha")"
    echo "updated" > "$live_dir/file.txt"
    mkdir -p "$live_dir/__pycache__"
    echo "bytecode" > "$live_dir/__pycache__/module.cpython-312.pyc"
    echo "pyc" > "$live_dir/module.pyc"
    echo "unrelated" > "$live_dir/.extra_artifact"

    local registry; registry="$(make_registry "$source_dir" "$live_dir")"
    reg_dir="$(install_registry "$registry")"

    local output exit_code
    output="$(MACHINE_LOCAL_REGISTRY_DIR="$reg_dir" HOME="$TMP_ROOT" python "$PROBE" 2>&1)"
    exit_code=$?

    assert_contains "untracked: [ok-via-git-propagation]" "[ok-via-git-propagation]" "$output" || { teardown_tmp; return 1; }
    assert_exit "untracked: exit 0" 0 "$exit_code" || { teardown_tmp; return 1; }
    teardown_tmp
}

# 10. per-machine-sentinels-present
# Live tree has .content-sentinel and version.txt alongside tracked content matching
# source HEAD. Git-ignored per-machine files must not appear in git ls-tree on SOURCE
# and must not cause false drift.
test_per_machine_sentinels_present() {
    setup_tmp
    setup_claude_home_git

    local source_dir live_dir reg_dir
    source_dir="$(make_source_repo_for_content_equiv "plugin/test-plugin")"
    local b_sha; b_sha="$(head_sha "$source_dir")"
    local a_sha; a_sha="$(git -C "$source_dir" rev-parse HEAD~1 2>/dev/null | tr -d '\r')"

    # Live dir: content matches B, sentinel at A, PLUS per-machine sentinel files.
    live_dir="$(make_live_dir "$a_sha")"
    echo "updated" > "$live_dir/file.txt"
    echo "some-content-hash" > "$live_dir/.content-sentinel"
    # version.txt is already written by make_live_dir (the $a_sha sentinel).

    local registry; registry="$(make_registry "$source_dir" "$live_dir")"
    reg_dir="$(install_registry "$registry")"

    local output exit_code
    output="$(MACHINE_LOCAL_REGISTRY_DIR="$reg_dir" HOME="$TMP_ROOT" python "$PROBE" 2>&1)"
    exit_code=$?

    assert_contains "sentinels: [ok-via-git-propagation]" "[ok-via-git-propagation]" "$output" || { teardown_tmp; return 1; }
    assert_exit "sentinels: exit 0" 0 "$exit_code" || { teardown_tmp; return 1; }
    teardown_tmp
}

# ---------------------------------------------------------------------------
# Run all tests
# ---------------------------------------------------------------------------

echo "=== test-check-plugin-drift-copy-install.sh ==="
echo ""

run_test "AC-2(d): clean equal SHA → [ok] exit 0"          test_clean_equal_sha
run_test "AC-2(e): drifted unequal SHA → [drift] exit 1"   test_drifted_unequal_sha
run_test "AC-2(b): no sentinel → [info] exit 0"             test_no_sentinel_info_exit0
run_test "AC-2(a): missing live path → [drift] exit 1"      test_missing_live_path
run_test "AC-3: --check-clean-only → exit 0"                test_check_clean_only_exit0
run_test "AC-2(c)/AC-6: malformed sentinel → [warn] exit 0" test_malformed_sentinel_warn_not_drift
run_test "AC-6: restore-orphan REPLACE semantics"           test_restore_orphan_gone
# New: content-equivalence fallback (blob-SHA mechanism)
run_test "AC1: sentinel-lagging-content-equivalent → [ok-via-git-propagation] exit 0" test_sentinel_lagging_content_equivalent
run_test "AC2: sentinel-lagging-content-differs → [drift] exit 1"                     test_sentinel_lagging_content_differs
run_test "AC2: sentinel-lagging-content-differs-many-files → cap 10 + N more exit 1"  test_sentinel_lagging_content_differs_many_files
run_test "AC3: sentinel-lagging-source-subpath-missing → [warn] exit 0"               test_sentinel_lagging_source_subpath_missing
run_test "AC3: sentinel-lagging-git-ls-tree-fails → [warn] exit 0"                    test_sentinel_lagging_git_ls_tree_fails
run_test "AC4: registry-source_subpath-explicit → [ok-via-git-propagation] exit 0"    test_registry_source_subpath_explicit
run_test "AC4: registry-source_subpath-default → [ok-via-git-propagation] exit 0"     test_registry_source_subpath_default
run_test "AC5/crlf: crlf-divergent-but-content-equal (autocrlf-aware)"                test_crlf_divergent_but_content_equal
run_test "AC5: untracked-artifacts-present → [ok-via-git-propagation] exit 0"         test_untracked_artifacts_present
run_test "AC5: per-machine-sentinels-present → [ok-via-git-propagation] exit 0"       test_per_machine_sentinels_present

echo ""
echo "Results: ${PASS} passed, ${FAIL} failed"

if [[ ${FAIL} -gt 0 ]]; then
    echo ""
    echo "Failed tests:"
    for msg in "${FAIL_MSGS[@]}"; do
        echo "  - $msg"
    done
    exit 1
fi

exit 0
