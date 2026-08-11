#!/usr/bin/env bash
# test-install-rendezvous-sweep.sh — fixture-sweep tests for the C1 rendezvous
# relocation in coordinator/templates/handoffs/continue-onboarding-and-installation.md.
#
# This test does NOT reimplement the sweep logic — it EXTRACTS the three ```bash
# code blocks (install-leg sweep, orient-leg second sweep, supersedes resolution
# sweep) verbatim out of the template and executes them against a sandboxed
# settings-home + legacy dir pair, so a future edit to the template's shell can't
# silently drift out of sync with what this test actually exercises.
#
# Coverage:
#   T1  install-leg baton at settings-home is discovered
#   T2  orient-leg baton at settings-home is discovered by the second sweep
#   T3  legacy-only baton (both classes) is discovered via the compat fallback
#   T4  a baton present in BOTH roots is de-duplicated to exactly one hit
#   T5  a supersedes: baton at settings-home is discovered by the third sweep,
#       and applying its declared supersession drops the named target from the
#       orient-leg spine set (per § Compat window / § Orientation-supersession)
#   T6  whole-file grep-clean: no non-compat `~/.claude/state/handoffs` primary
#       reference remains anywhere in the template (frontmatter glob + the
#       install-final-message pickup path included)
#
# Exit 0 == all tests passed.
#
# Spec backlink: docs/plans/2026-07-08-install-baton-rendezvous-off-dotclaude.md § C1

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TEMPLATE="$SCRIPT_DIR/../../templates/handoffs/continue-onboarding-and-installation.md"

PASS=0
FAIL=0

_pass() { printf '  PASS: %s\n' "$1"; PASS=$((PASS + 1)); }
_fail() { printf '  FAIL: %s — %s\n' "$1" "$2"; FAIL=$((FAIL + 1)); }

echo "=== test-install-rendezvous-sweep.sh ==="

if [[ ! -f "$TEMPLATE" ]]; then
  echo "FATAL: template not found: $TEMPLATE" >&2
  exit 1
fi

# ---------------------------------------------------------------------------
# Block extraction — pull a ```bash ... ``` fence out of the template verbatim,
# starting at the line containing $2 (a unique substring inside that block) and
# running until the next closing fence, dedenting the 3-space list-item indent.
# ---------------------------------------------------------------------------
extract_block() {
  local file="$1" marker="$2"
  awk -v marker="$marker" '
    index($0, marker) { on = 1 }
    on && /^[[:space:]]*```[[:space:]]*$/ { exit }
    on {
      line = $0
      sub(/^   /, "", line)
      print line
    }
  ' "$file"
}

INSTALL_LEG_BLOCK="$(extract_block "$TEMPLATE" '# Resolve the rendezvous through the seam, with an inline fallback for a cold tool shell')"
ORIENT_LEG_BLOCK="$(extract_block "$TEMPLATE" '# Orient-leg discovery')"
SUPERSEDES_BLOCK="$(extract_block "$TEMPLATE" '# Generic over supersedes:<any-id>')"

for _name_block in "INSTALL_LEG_BLOCK:$INSTALL_LEG_BLOCK" "ORIENT_LEG_BLOCK:$ORIENT_LEG_BLOCK" "SUPERSEDES_BLOCK:$SUPERSEDES_BLOCK"; do
  _name="${_name_block%%:*}"
  _body="${_name_block#*:}"
  if [[ -z "$_body" ]]; then
    echo "FATAL: extraction of $_name produced an empty block — marker text drifted out of sync with the template" >&2
    exit 1
  fi
done

# ---------------------------------------------------------------------------
# Sandbox
# ---------------------------------------------------------------------------
_TMPDIR="$(mktemp -d)"
_cleanup() { rm -rf "$_TMPDIR"; }
trap _cleanup EXIT

FAKE_CLAUDE_HOME="$_TMPDIR/fakehome"
FAKE_SETTINGS_HOME="$_TMPDIR/settings-home"
NEW_HANDOFFS_DIR="$FAKE_SETTINGS_HOME/state/handoffs"
LEGACY_HANDOFFS_DIR="$FAKE_CLAUDE_HOME/.claude/state/handoffs"
mkdir -p "$NEW_HANDOFFS_DIR" "$LEGACY_HANDOFFS_DIR"

# Fake `coordinator-settings-home` resolver on PATH so the extracted blocks'
# `command -v coordinator-settings-home` branch resolves to our sandbox, never
# falling through to the inline fallback (that fallback is exercised by
# test-settings-home.sh, not here).
FAKE_BIN_DIR="$_TMPDIR/bin"
mkdir -p "$FAKE_BIN_DIR"
cat > "$FAKE_BIN_DIR/coordinator-settings-home" <<EOF
#!/usr/bin/env bash
echo "$FAKE_SETTINGS_HOME"
EOF
chmod +x "$FAKE_BIN_DIR/coordinator-settings-home"

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

# T1 fixture — install-leg baton at settings-home ONLY.
cat > "$NEW_HANDOFFS_DIR/install-example-retrieval-repo.md" <<'EOF'
---
kind: spinoff
repo: example-retrieval-repo
install_chain_order: 2
---
EOF

# T2 fixture — orient-leg baton at settings-home ONLY.
cat > "$NEW_HANDOFFS_DIR/orient-example-retrieval-repo.md" <<'EOF'
---
kind: spinoff
title: "Orient example-retrieval-repo"
---
EOF

# T3 fixture — legacy-only batons (both classes), NOT present at settings-home.
cat > "$LEGACY_HANDOFFS_DIR/install-cockpit.md" <<'EOF'
---
kind: spinoff
repo: cockpit
install_chain_order: 6
---
EOF
cat > "$LEGACY_HANDOFFS_DIR/orient-cockpit.md" <<'EOF'
---
kind: spinoff
title: "Orient cockpit"
---
EOF

# T4 fixture — same repo+install_chain_order present at BOTH roots (dedup case).
cat > "$NEW_HANDOFFS_DIR/install-example-game-repo.md" <<'EOF'
---
kind: spinoff
repo: example-game-repo
install_chain_order: 4
---
EOF
cat > "$LEGACY_HANDOFFS_DIR/install-example-game-repo.md" <<'EOF'
---
kind: spinoff
repo: example-game-repo
install_chain_order: 4
---
EOF

# T5 fixture — an orient-leg target, plus a settings-home-only baton that
# supersedes it.
cat > "$NEW_HANDOFFS_DIR/orient-target.md" <<'EOF'
---
kind: spinoff
title: "orientation for target"
---
EOF
cat > "$NEW_HANDOFFS_DIR/orient-supersedes-target.md" <<'EOF'
---
kind: spinoff
supersedes: orient-target
title: "orientation superseding the target"
---
EOF

# ---------------------------------------------------------------------------
# Runner — executes an extracted block with PATH/CLAUDE_HOME pointed at the
# sandbox, so `coordinator-settings-home` resolves to FAKE_SETTINGS_HOME and
# the LEGACY_HANDOFFS fallback resolves to FAKE_CLAUDE_HOME/.claude/state/handoffs.
# ---------------------------------------------------------------------------
run_block() {
  local block="$1"
  env -i \
    PATH="$FAKE_BIN_DIR:/usr/bin:/bin" \
    CLAUDE_HOME="$FAKE_CLAUDE_HOME" \
    HOME="$FAKE_CLAUDE_HOME" \
    /opt/homebrew/bin/bash -c "$block"
}

# --- T1: install-leg baton at settings-home discovered ---
INSTALL_LEG_OUT="$(run_block "$INSTALL_LEG_BLOCK")"
if grep -q "install-example-retrieval-repo.md" <<<"$INSTALL_LEG_OUT"; then
  _pass "T1 install-leg baton at settings-home discovered"
else
  _fail "T1 install-leg baton at settings-home discovered" "not found in: $INSTALL_LEG_OUT"
fi

# --- T2: orient-leg baton at settings-home discovered by the second sweep ---
ORIENT_LEG_OUT="$(run_block "$ORIENT_LEG_BLOCK")"
if grep -q "orient-example-retrieval-repo.md" <<<"$ORIENT_LEG_OUT"; then
  _pass "T2 orient-leg baton at settings-home discovered"
else
  _fail "T2 orient-leg baton at settings-home discovered" "not found in: $ORIENT_LEG_OUT"
fi

# --- T3: legacy-only baton (both classes) discovered via fallback ---
if grep -q "install-cockpit.md" <<<"$INSTALL_LEG_OUT"; then
  _pass "T3a legacy-only install-leg baton discovered via fallback"
else
  _fail "T3a legacy-only install-leg baton discovered via fallback" "not found in: $INSTALL_LEG_OUT"
fi
if grep -q "orient-cockpit.md" <<<"$ORIENT_LEG_OUT"; then
  _pass "T3b legacy-only orient-leg baton discovered via fallback"
else
  _fail "T3b legacy-only orient-leg baton discovered via fallback" "not found in: $ORIENT_LEG_OUT"
fi

# --- T4: a baton present in BOTH roots is de-duplicated to exactly one hit ---
EXAMPLE_GAME_REPO_HITS="$(grep -c "install-example-game-repo.md" <<<"$INSTALL_LEG_OUT" || true)"
if [[ "$EXAMPLE_GAME_REPO_HITS" -eq 1 ]]; then
  _pass "T4 both-located baton de-duplicated to exactly one hit"
else
  _fail "T4 both-located baton de-duplicated to exactly one hit" "expected 1 hit, got $EXAMPLE_GAME_REPO_HITS in: $INSTALL_LEG_OUT"
fi

# --- T5: supersedes baton discovered, and applying it drops the target from the orient set ---
SUPERSEDES_OUT="$(run_block "$SUPERSEDES_BLOCK")"
if grep -q "orient-supersedes-target.md" <<<"$SUPERSEDES_OUT"; then
  _pass "T5a supersedes: baton at settings-home discovered"
else
  _fail "T5a supersedes: baton at settings-home discovered" "not found in: $SUPERSEDES_OUT"
fi

# Apply the discovered supersession the same way the template's Step 0.4
# instructs the picking-up EM to (read supersedes:, drop the named target from
# the spine) — this simulates the doc-described apply step, not new production
# logic, so the test can assert the end-to-end "drops its target" behavior.
SUPERSEDING_FILE="$(grep 'orient-supersedes-target.md' <<<"$SUPERSEDES_OUT" | head -n1)"
SUPERSEDES_TARGET="$(grep -m1 '^supersedes:' "$SUPERSEDING_FILE" | sed 's/^supersedes:[[:space:]]*//')"
if [[ "$SUPERSEDES_TARGET" == "orient-target" ]]; then
  _pass "T5b supersedes: field correctly names its target (orient-target)"
else
  _fail "T5b supersedes: field correctly names its target (orient-target)" "got: $SUPERSEDES_TARGET"
fi

FINAL_ORIENT_SET="$(grep -v "orient-${SUPERSEDES_TARGET#orient-}.md" <<<"$ORIENT_LEG_OUT" || true)"
if ! grep -q "orient-target.md" <<<"$FINAL_ORIENT_SET"; then
  _pass "T5c applying the supersedes: baton drops orient-target from the spine"
else
  _fail "T5c applying the supersedes: baton drops orient-target from the spine" "orient-target.md still present in: $FINAL_ORIENT_SET"
fi

# ---------------------------------------------------------------------------
# T6: whole-file grep-clean — no non-compat ~/.claude/state/handoffs primary
# reference remains anywhere in the template.
# ---------------------------------------------------------------------------

# T6a — the frontmatter scope glob resolves through the seam.
if grep -q '^\s*-\s*\$(coordinator-settings-home)/state/handoffs/\*\*' "$TEMPLATE"; then
  _pass "T6a frontmatter scope glob repointed to the seam"
else
  _fail "T6a frontmatter scope glob repointed to the seam" "expected a scope entry naming \$(coordinator-settings-home)/state/handoffs/**"
fi

# T6b — the install-final-message pickup path (and the Notes pickup path) are
# repointed; the OLD bare form must not survive anywhere in the file.
if grep -q '/pickup state/handoffs/continue-onboarding-and-installation.md' "$TEMPLATE"; then
  _fail "T6b old bare pickup path fully retired" "found a residual '/pickup state/handoffs/continue-onboarding-and-installation.md' reference"
else
  _pass "T6b old bare pickup path fully retired"
fi
PICKUP_HITS="$(grep -c '/pickup \$(coordinator-settings-home)/state/handoffs/continue-onboarding-and-installation.md' "$TEMPLATE" || true)"
if [[ "$PICKUP_HITS" -ge 2 ]]; then
  _pass "T6c both pickup-path mentions (Notes + install-final-message) repointed to the seam"
else
  _fail "T6c both pickup-path mentions (Notes + install-final-message) repointed to the seam" "expected >=2 occurrences, got $PICKUP_HITS"
fi

# T6d — every remaining legacy-form reference (~/.claude/state/handoffs or the
# ${CLAUDE_HOME:-$HOME}/.claude/state/handoffs shell expression) is an
# explicitly-labeled compat-fallback reference, not a silent primary read.
# "Labeled" = the matching line (or the 3 lines immediately preceding it, to
# catch prose whose "legacy"/"compat" qualifier sits on the sentence-opening
# line above) mentions LEGACY_HANDOFFS or the word "legacy"/"compat" (case-insensitive).
mapfile -t LEGACY_LINE_NUMS < <(grep -n '\.claude/state/handoffs' "$TEMPLATE" | cut -d: -f1)
if [[ "${#LEGACY_LINE_NUMS[@]}" -eq 0 ]]; then
  _fail "T6d every legacy-form reference is a labeled compat fallback" "no legacy-form references found at all — suspicious (expected the 3 LEGACY_HANDOFFS= assignments + the compat-window prose sentence)"
else
  _all_labeled=1
  for _ln in "${LEGACY_LINE_NUMS[@]}"; do
    _start=$(( _ln - 3 ))
    [[ $_start -lt 1 ]] && _start=1
    _context="$(sed -n "${_start},${_ln}p" "$TEMPLATE")"
    if ! grep -qiE 'legacy|compat' <<<"$_context"; then
      _all_labeled=0
      _fail "T6d every legacy-form reference is a labeled compat fallback" "line $_ln has no legacy/compat label in its context: $_context"
    fi
  done
  if [[ "$_all_labeled" -eq 1 ]]; then
    _pass "T6d every legacy-form reference (${#LEGACY_LINE_NUMS[@]} found) is a labeled compat fallback"
  fi
fi

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
echo ""
echo "=== Results: $PASS passed, $FAIL failed ==="
if [[ "$FAIL" -gt 0 ]]; then
  exit 1
fi
exit 0
