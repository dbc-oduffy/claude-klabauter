#!/usr/bin/env bash
# test-seed-skill-overrides.sh — verify seed-skill-overrides.py (helper) and
# the install-health drop-in seed-skill-overrides.sh.
#
# Ownership note (DR-079, 2026-07-21): the install-health drop-in this test
# exercises (bin/install-health/seed-skill-overrides.sh) is claude-klabauter-owned per
# DR-079 tool 8 — install-engine probes, ported and eventually run by claude-klabauter's
# own install_health_run.py. Claude-klabauter is actively porting this glob's members;
# do NOT naked-Python-port the drop-in from example-doctrine-repo — that duplicates
# claude-klabauter's work and crosses the tri-plane subject-matter boundary. (The
# sibling helper, bin/seed-skill-overrides.py, is a separate example-doctrine-repo-owned CLI —
# not in scope of this ownership note.) This test stays example-doctrine-repo-side (and green)
# against current disk reality until the claude-klabauter port lands and the
# caller-repoint memo retires the example-doctrine-repo-side copy.
#
# set -uo pipefail (NOT -e): all cases run; exit codes captured manually.
# Isolation: every case uses its own mktemp -d; NEVER touches real ~/.claude.
# Spec backlink: docs/plans/2026-06-27-ccos-1-dual-context-validator.md
#
# All python3 invocations below are macOS bash-level shell calls to the Python
# interpreter — no Windows AllocConsole concern applies to this script.
#
# DROP_IN invocation (2026-07-22 F1 fix): the drop-in keeps a `.sh` suffix by
# design (see its own header) so install-health-run.py's glob still finds it,
# but its shebang is `#!/usr/bin/env python3` — it is naked Python wearing a
# `.sh` name, not a shell script. Cases 9/9b/9c invoke it via `python3
# "${DROP_IN}"`, not `bash`.

set -uo pipefail

# ---------------------------------------------------------------------------
# Resolve artifacts — COORDINATOR_ROOT set explicitly so the path is always
# unambiguous from any invocation location.
# ---------------------------------------------------------------------------
COORDINATOR_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
HELPER="${COORDINATOR_ROOT}/bin/seed-skill-overrides.py"
DROP_IN="${COORDINATOR_ROOT}/bin/install-health/seed-skill-overrides.sh"

PASS=0
FAIL=0

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
fail() {
    local name="$1" reason="${2:-}"
    echo "FAIL: ${name}${reason:+ — ${reason}}"
    FAIL=$(( FAIL + 1 ))
}

pass() {
    echo "PASS: $1"
    PASS=$(( PASS + 1 ))
}

# SHA-256 of a file, via python3 (avoids md5/md5sum portability difference).
file_hash() {
    python3 -c "import hashlib,sys; print(hashlib.sha256(open(sys.argv[1],'rb').read()).hexdigest())" "$1" # popup-safe-env-suppressed
}

# Guard: abort if the artifacts are absent (prevents misleading all-pass on a
# bad root).
for _f in "${HELPER}" "${DROP_IN}"; do
    if [[ ! -f "${_f}" ]]; then
        echo "FATAL: artifact not found: ${_f}" >&2
        exit 2
    fi
done

# ---------------------------------------------------------------------------
# Case 1: Fresh seed (helper) — AC1
# Empty CLAUDE_HOME; helper (no flags) must create settings.json with exactly
# review/security-review/simplify/init each = "off"; deep-research absent.
# ---------------------------------------------------------------------------
C="Case 1: Fresh seed (helper)"
_tmp=$(mktemp -d)
_rc=0
CLAUDE_HOME="${_tmp}" python3 "${HELPER}" >/dev/null 2>&1 || _rc=$? # popup-safe-env-suppressed
_settings="${_tmp}/.claude/settings.json"
if [[ ${_rc} -ne 0 ]]; then
    fail "${C}" "helper exited ${_rc}"
elif [[ ! -f "${_settings}" ]]; then
    fail "${C}" "settings.json not created"
else
    _result=$(python3 - "${_settings}" <<'PYEOF' # popup-safe-env-suppressed
import json, sys
data = json.load(open(sys.argv[1]))
overrides = data.get("skillOverrides", {})
expected = {"review", "security-review", "simplify", "init"}
actual = set(overrides.keys())
errors = []
if actual != expected:
    errors.append("keys=" + str(sorted(actual)) + " want=" + str(sorted(expected)))
for k, v in overrides.items():
    if v != "off":
        errors.append(k + "=" + repr(v) + " want off")
if "deep-research" in overrides:
    errors.append("deep-research should be absent")
print("ERRORS:" + "; ".join(errors) if errors else "OK")
PYEOF
    )
    if [[ "${_result}" == "OK" ]]; then pass "${C}"; else fail "${C}" "${_result}"; fi
fi
rm -rf "${_tmp}"

# ---------------------------------------------------------------------------
# Case 2: Non-clobber (helper) — AC2
# Pre-write settings with review="name-only" and env.X="1"; after run:
# review must still be "name-only", env.X must still be "1", and
# simplify/init/security-review must have been added as "off".
# ---------------------------------------------------------------------------
C="Case 2: Non-clobber (helper)"
_tmp=$(mktemp -d)
mkdir -p "${_tmp}/.claude"
_settings="${_tmp}/.claude/settings.json"
printf '{"skillOverrides":{"review":"name-only"},"env":{"X":"1"}}' > "${_settings}"
_rc=0
CLAUDE_HOME="${_tmp}" python3 "${HELPER}" >/dev/null 2>&1 || _rc=$? # popup-safe-env-suppressed
if [[ ${_rc} -ne 0 ]]; then
    fail "${C}" "helper exited ${_rc}"
else
    _result=$(python3 - "${_settings}" <<'PYEOF' # popup-safe-env-suppressed
import json, sys
data = json.load(open(sys.argv[1]))
overrides = data.get("skillOverrides", {})
errors = []
if overrides.get("review") != "name-only":
    errors.append("review=" + repr(overrides.get("review")) + " want name-only")
if data.get("env", {}).get("X") != "1":
    errors.append("env.X=" + repr(data.get("env", {}).get("X")) + " want 1")
for k in ("simplify", "init", "security-review"):
    if overrides.get(k) != "off":
        errors.append(k + "=" + repr(overrides.get(k)) + " want off")
print("ERRORS:" + "; ".join(errors) if errors else "OK")
PYEOF
    )
    if [[ "${_result}" == "OK" ]]; then pass "${C}"; else fail "${C}" "${_result}"; fi
fi
rm -rf "${_tmp}"

# ---------------------------------------------------------------------------
# Case 3: --with-deep-research (helper) — AC3
# Part A: flag present → deep-research="off" in settings.
# Part B: flag absent  → deep-research key absent in settings.
# ---------------------------------------------------------------------------
C="Case 3: --with-deep-research (helper)"
# Part A
_tmp=$(mktemp -d)
_rc=0
CLAUDE_HOME="${_tmp}" python3 "${HELPER}" --with-deep-research >/dev/null 2>&1 || _rc=$? # popup-safe-env-suppressed
_settings="${_tmp}/.claude/settings.json"
_part_a="OK"
if [[ ${_rc} -ne 0 ]]; then
    _part_a="exit ${_rc} (with flag)"
elif [[ ! -f "${_settings}" ]]; then
    _part_a="settings not created"
else
    _part_a=$(python3 - "${_settings}" <<'PYEOF' # popup-safe-env-suppressed
import json, sys
overrides = json.load(open(sys.argv[1])).get("skillOverrides", {})
if "deep-research" not in overrides:
    print("deep-research absent")
elif overrides["deep-research"] != "off":
    print("deep-research=" + repr(overrides["deep-research"]) + " want off")
else:
    print("OK")
PYEOF
    )
fi
rm -rf "${_tmp}"

# Part B
_tmp=$(mktemp -d)
_rc=0
CLAUDE_HOME="${_tmp}" python3 "${HELPER}" >/dev/null 2>&1 || _rc=$? # popup-safe-env-suppressed
_settings="${_tmp}/.claude/settings.json"
_part_b="OK"
if [[ ${_rc} -ne 0 ]]; then
    _part_b="exit ${_rc} (without flag)"
elif [[ ! -f "${_settings}" ]]; then
    _part_b="settings not created"
else
    _part_b=$(python3 - "${_settings}" <<'PYEOF' # popup-safe-env-suppressed
import json, sys
overrides = json.load(open(sys.argv[1])).get("skillOverrides", {})
print("OK" if "deep-research" not in overrides else "deep-research should be absent")
PYEOF
    )
fi
rm -rf "${_tmp}"

if [[ "${_part_a}" == "OK" && "${_part_b}" == "OK" ]]; then
    pass "${C}"
else
    fail "${C}" "with-flag: ${_part_a}; without-flag: ${_part_b}"
fi

# ---------------------------------------------------------------------------
# Case 4: Idempotent (helper) — AC4
# Run helper twice; settings.json must be byte-identical between the two runs.
# ---------------------------------------------------------------------------
C="Case 4: Idempotent (helper)"
_tmp=$(mktemp -d)
_settings="${_tmp}/.claude/settings.json"
_rc1=0; _rc2=0
CLAUDE_HOME="${_tmp}" python3 "${HELPER}" >/dev/null 2>&1 || _rc1=$? # popup-safe-env-suppressed
_hash1=""
[[ -f "${_settings}" ]] && _hash1=$(file_hash "${_settings}")
CLAUDE_HOME="${_tmp}" python3 "${HELPER}" >/dev/null 2>&1 || _rc2=$? # popup-safe-env-suppressed
_hash2=""
[[ -f "${_settings}" ]] && _hash2=$(file_hash "${_settings}")
if [[ ${_rc1} -ne 0 || ${_rc2} -ne 0 ]]; then
    fail "${C}" "rc1=${_rc1} rc2=${_rc2}"
elif [[ -z "${_hash1}" || -z "${_hash2}" ]]; then
    fail "${C}" "settings.json not created"
elif [[ "${_hash1}" != "${_hash2}" ]]; then
    fail "${C}" "run1 and run2 hashes differ (not idempotent)"
else
    pass "${C}"
fi
rm -rf "${_tmp}"

# ---------------------------------------------------------------------------
# Case 5: Malformed settings → exit 1, no write (helper) — AC5
# Write '{invalid json' to settings.json; helper must exit != 0 and leave
# the file byte-identical to the malformed input.
# ---------------------------------------------------------------------------
C="Case 5: Malformed settings → exit 1 no write (helper)"
_tmp=$(mktemp -d)
mkdir -p "${_tmp}/.claude"
_settings="${_tmp}/.claude/settings.json"
printf '{invalid json' > "${_settings}"
_hash_before=$(file_hash "${_settings}")
_rc=0
CLAUDE_HOME="${_tmp}" python3 "${HELPER}" >/dev/null 2>&1 || _rc=$? # popup-safe-env-suppressed
_hash_after=$(file_hash "${_settings}")
if [[ ${_rc} -eq 0 ]]; then
    fail "${C}" "expected non-zero exit, got 0"
elif [[ "${_hash_before}" != "${_hash_after}" ]]; then
    fail "${C}" "file was modified despite malformed input"
else
    pass "${C}"
fi
rm -rf "${_tmp}"

# ---------------------------------------------------------------------------
# Case 6: Sibling keys preserved (helper) — AC4 / non-clobber
# Settings with env, permissions, enabledPlugins keys → all three must survive
# the helper run unchanged.
# ---------------------------------------------------------------------------
C="Case 6: Sibling keys preserved (helper)"
_tmp=$(mktemp -d)
mkdir -p "${_tmp}/.claude"
_settings="${_tmp}/.claude/settings.json"
python3 - "${_settings}" <<'PYEOF' # popup-safe-env-suppressed
import json, sys
data = {
    "env": {"PATH_EXT": "/usr/local/bin"},
    "permissions": ["read", "write"],
    "enabledPlugins": ["coordinator", "deep-research"],
    "skillOverrides": {}
}
open(sys.argv[1], "w", newline="\n").write(json.dumps(data, indent=2) + "\n")
PYEOF
_rc=0
CLAUDE_HOME="${_tmp}" python3 "${HELPER}" >/dev/null 2>&1 || _rc=$? # popup-safe-env-suppressed
if [[ ${_rc} -ne 0 ]]; then
    fail "${C}" "helper exited ${_rc}"
else
    _result=$(python3 - "${_settings}" <<'PYEOF' # popup-safe-env-suppressed
import json, sys
data = json.load(open(sys.argv[1]))
errors = []
if data.get("env") != {"PATH_EXT": "/usr/local/bin"}:
    errors.append("env changed: " + repr(data.get("env")))
if data.get("permissions") != ["read", "write"]:
    errors.append("permissions changed: " + repr(data.get("permissions")))
if data.get("enabledPlugins") != ["coordinator", "deep-research"]:
    errors.append("enabledPlugins changed: " + repr(data.get("enabledPlugins")))
print("ERRORS:" + "; ".join(errors) if errors else "OK")
PYEOF
    )
    if [[ "${_result}" == "OK" ]]; then pass "${C}"; else fail "${C}" "${_result}"; fi
fi
rm -rf "${_tmp}"

# ---------------------------------------------------------------------------
# Case 7: CLAUDE_HOME home-substitute footgun guard (helper) — AC7
# CLAUDE_HOME=/tmp/x → settings at /tmp/x/.claude/settings.json
# NOT at /tmp/x/settings.json.
# ---------------------------------------------------------------------------
C="Case 7: CLAUDE_HOME home-substitute (helper)"
_tmp=$(mktemp -d)
_rc=0
CLAUDE_HOME="${_tmp}" python3 "${HELPER}" >/dev/null 2>&1 || _rc=$? # popup-safe-env-suppressed
_correct="${_tmp}/.claude/settings.json"
_wrong="${_tmp}/settings.json"
if [[ ${_rc} -ne 0 ]]; then
    fail "${C}" "helper exited ${_rc}"
elif [[ ! -f "${_correct}" ]]; then
    fail "${C}" "settings.json not found at expected path (${_correct})"
elif [[ -f "${_wrong}" ]]; then
    fail "${C}" "settings.json found at WRONG path (${_wrong}) — footgun not guarded"
else
    pass "${C}"
fi
rm -rf "${_tmp}"

# ---------------------------------------------------------------------------
# Case 8: --check-only no write (helper) — AC9
# Pre-seed an empty '{}' settings.json; run helper --check-only → exit 0,
# stdout must contain "(check-only)", file must be byte-identical afterward.
# ---------------------------------------------------------------------------
C="Case 8: --check-only no write (helper)"
_tmp=$(mktemp -d)
mkdir -p "${_tmp}/.claude"
_settings="${_tmp}/.claude/settings.json"
printf '{}' > "${_settings}"
_hash_before=$(file_hash "${_settings}")
_rc=0
_stdout=$(CLAUDE_HOME="${_tmp}" python3 "${HELPER}" --check-only 2>/dev/null) || _rc=$? # popup-safe-env-suppressed
_hash_after=$(file_hash "${_settings}")
if [[ ${_rc} -ne 0 ]]; then
    fail "${C}" "expected exit 0, got ${_rc}"
elif [[ "${_hash_before}" != "${_hash_after}" ]]; then
    fail "${C}" "file was modified under --check-only"
elif [[ "${_stdout}" != *"(check-only)"* ]]; then
    fail "${C}" "stdout lacks '(check-only)': ${_stdout}"
else
    pass "${C}"
fi
rm -rf "${_tmp}"

# ---------------------------------------------------------------------------
# Case 8b: --check-only already-seeded (helper) — AC9
# Pre-seed all four core overrides present; run helper --check-only → exit 0,
# stdout must indicate nothing-to-seed ("(none)"), file must be byte-unchanged.
# ---------------------------------------------------------------------------
C="Case 8b: --check-only already-seeded (helper)"
_tmp=$(mktemp -d)
mkdir -p "${_tmp}/.claude"
_settings="${_tmp}/.claude/settings.json"
python3 - "${_settings}" <<'PYEOF' # popup-safe-env-suppressed
import json, sys
data = {"skillOverrides": {"review": "off", "security-review": "off", "simplify": "off", "init": "off"}}
open(sys.argv[1], "w", newline="\n").write(json.dumps(data, indent=2) + "\n")
PYEOF
_hash_before=$(file_hash "${_settings}")
_rc=0
_stdout=$(CLAUDE_HOME="${_tmp}" python3 "${HELPER}" --check-only 2>/dev/null) || _rc=$? # popup-safe-env-suppressed
_hash_after=$(file_hash "${_settings}")
if [[ ${_rc} -ne 0 ]]; then
    fail "${C}" "expected exit 0, got ${_rc}"
elif [[ "${_hash_before}" != "${_hash_after}" ]]; then
    fail "${C}" "file was modified under --check-only"
elif [[ "${_stdout}" != *"(none)"* ]]; then
    fail "${C}" "stdout lacks '(none)' (want nothing-to-seed): ${_stdout}"
else
    pass "${C}"
fi
rm -rf "${_tmp}"

# ---------------------------------------------------------------------------
# Case 9: DROP-IN detection seam — regression guard — AC5
# Run drop-in under temp CLAUDE_HOME with NO deep-research plugin → no
# deep-research key in settings. Then create the detection sentinel (web.md)
# and run again → deep-research key now present.
# This exercises detection end-to-end through the .sh, not just a string grep.
# ---------------------------------------------------------------------------
C="Case 9: DROP-IN detection seam"
_tmp=$(mktemp -d)
_settings="${_tmp}/.claude/settings.json"
# Plant .doe-root so the trusted-prefix guard accepts COORDINATOR_ROOT
mkdir -p "${_tmp}/.claude"
printf '%s' "$(dirname "${COORDINATOR_ROOT}")" > "${_tmp}/.claude/.doe-root"

# First run: deep-research NOT installed
_rc=0
CLAUDE_HOME="${_tmp}" CLAUDE_PLUGIN_ROOT="${COORDINATOR_ROOT}" \
    python3 "${DROP_IN}" >/dev/null 2>&1 || _rc=$?
_part_a="OK"
if [[ ${_rc} -ne 0 ]]; then
    _part_a="drop-in exited ${_rc} on first run"
elif [[ ! -f "${_settings}" ]]; then
    _part_a="settings.json not created on first run"
else
    _part_a=$(python3 - "${_settings}" <<'PYEOF' # popup-safe-env-suppressed
import json, sys
overrides = json.load(open(sys.argv[1])).get("skillOverrides", {})
errors = []
# deep-research is always bundled post-Wave-C4; expect it on first run too
expected = {"review", "security-review", "simplify", "init", "deep-research"}
actual = set(overrides.keys())
if actual != expected:
    errors.append("keys=" + str(sorted(actual)) + " want=" + str(sorted(expected)))
if "deep-research" in overrides and overrides["deep-research"] != "off":
    errors.append("deep-research=" + repr(overrides["deep-research"]) + " want off")
print("ERRORS:" + "; ".join(errors) if errors else "OK")
PYEOF
    )
fi

# Plant the deep-research sentinel the drop-in checks for (standard install path).
_dr_dir="${_tmp}/.claude/plugins/deep-research/commands"
mkdir -p "${_dr_dir}"
printf 'stub' > "${_dr_dir}/web.md"

# Second run: deep-research IS detected
_rc=0
CLAUDE_HOME="${_tmp}" CLAUDE_PLUGIN_ROOT="${COORDINATOR_ROOT}" \
    python3 "${DROP_IN}" >/dev/null 2>&1 || _rc=$?
_part_b="OK"
if [[ ${_rc} -ne 0 ]]; then
    _part_b="drop-in exited ${_rc} on second run"
elif [[ ! -f "${_settings}" ]]; then
    _part_b="settings.json missing after second run"
else
    _part_b=$(python3 - "${_settings}" <<'PYEOF' # popup-safe-env-suppressed
import json, sys
overrides = json.load(open(sys.argv[1])).get("skillOverrides", {})
errors = []
required = {"review", "security-review", "simplify", "init", "deep-research"}
missing = required - set(overrides.keys())
if missing:
    errors.append("missing keys: " + str(sorted(missing)))
if "deep-research" in overrides and overrides["deep-research"] != "off":
    errors.append("deep-research=" + repr(overrides["deep-research"]) + " want off")
print("ERRORS:" + "; ".join(errors) if errors else "OK")
PYEOF
    )
fi
rm -rf "${_tmp}"

if [[ "${_part_a}" == "OK" && "${_part_b}" == "OK" ]]; then
    pass "${C}"
else
    fail "${C}" "no-dr: ${_part_a}; with-dr: ${_part_b}"
fi

# ---------------------------------------------------------------------------
# Case 9b: DROP-IN standard-path detection — AC5 variant
# Plant sentinel at ${tmp}/.claude/plugins/deep-research/commands/web.md
# (the standard install path added by impl-F1) → deep-research seeded.
# ---------------------------------------------------------------------------
C="Case 9b: DROP-IN standard-path detection"
_tmp=$(mktemp -d)
_settings="${_tmp}/.claude/settings.json"
# Plant .doe-root so the trusted-prefix guard accepts COORDINATOR_ROOT
mkdir -p "${_tmp}/.claude"
printf '%s' "$(dirname "${COORDINATOR_ROOT}")" > "${_tmp}/.claude/.doe-root"

_dr_dir="${_tmp}/.claude/plugins/deep-research/commands"
mkdir -p "${_dr_dir}"
printf 'stub' > "${_dr_dir}/web.md"

_rc=0
CLAUDE_HOME="${_tmp}" CLAUDE_PLUGIN_ROOT="${COORDINATOR_ROOT}" \
    python3 "${DROP_IN}" >/dev/null 2>&1 || _rc=$?
if [[ ${_rc} -ne 0 ]]; then
    fail "${C}" "drop-in exited ${_rc}"
elif [[ ! -f "${_settings}" ]]; then
    fail "${C}" "settings.json not created"
else
    _result=$(python3 - "${_settings}" <<'PYEOF' # popup-safe-env-suppressed
import json, sys
overrides = json.load(open(sys.argv[1])).get("skillOverrides", {})
if "deep-research" not in overrides:
    print("deep-research absent; want present (standard-path detection)")
elif overrides["deep-research"] != "off":
    print("deep-research=" + repr(overrides["deep-research"]) + " want off")
else:
    print("OK")
PYEOF
    )
    if [[ "${_result}" == "OK" ]]; then pass "${C}"; else fail "${C}" "${_result}"; fi
fi
rm -rf "${_tmp}"

# ---------------------------------------------------------------------------
# Case 9c: DROP-IN cache-glob detection — AC5 variant
# Plant sentinel at ${tmp}/.claude/plugins/cache/<ver>/deep-research/<id>/commands/web.md
# (the cache checkout path matched by the ls-glob in the drop-in) → deep-research seeded.
# ---------------------------------------------------------------------------
C="Case 9c: DROP-IN cache-glob detection"
_tmp=$(mktemp -d)
_settings="${_tmp}/.claude/settings.json"
# Plant .doe-root so the trusted-prefix guard accepts COORDINATOR_ROOT
mkdir -p "${_tmp}/.claude"
printf '%s' "$(dirname "${COORDINATOR_ROOT}")" > "${_tmp}/.claude/.doe-root"

_dr_cache_dir="${_tmp}/.claude/plugins/cache/1.0.0/deep-research/abc123/commands"
mkdir -p "${_dr_cache_dir}"
printf 'stub' > "${_dr_cache_dir}/web.md"

_rc=0
CLAUDE_HOME="${_tmp}" CLAUDE_PLUGIN_ROOT="${COORDINATOR_ROOT}" \
    python3 "${DROP_IN}" >/dev/null 2>&1 || _rc=$?
if [[ ${_rc} -ne 0 ]]; then
    fail "${C}" "drop-in exited ${_rc}"
elif [[ ! -f "${_settings}" ]]; then
    fail "${C}" "settings.json not created"
else
    _result=$(python3 - "${_settings}" <<'PYEOF' # popup-safe-env-suppressed
import json, sys
overrides = json.load(open(sys.argv[1])).get("skillOverrides", {})
if "deep-research" not in overrides:
    print("deep-research absent; want present (cache-glob detection)")
elif overrides["deep-research"] != "off":
    print("deep-research=" + repr(overrides["deep-research"]) + " want off")
else:
    print("OK")
PYEOF
    )
    if [[ "${_result}" == "OK" ]]; then pass "${C}"; else fail "${C}" "${_result}"; fi
fi
rm -rf "${_tmp}"

# ---------------------------------------------------------------------------
# Case 10: Non-dict skillOverrides → exit 1, no write (helper) — AC4
# Write '{"skillOverrides": null}' (valid JSON, wrong shape); helper must
# exit != 0 and leave the file byte-identical.
# ---------------------------------------------------------------------------
C="Case 10: Non-dict skillOverrides → exit 1 no write (helper)"
_tmp=$(mktemp -d)
mkdir -p "${_tmp}/.claude"
_settings="${_tmp}/.claude/settings.json"
printf '{"skillOverrides": null}' > "${_settings}"
_hash_before=$(file_hash "${_settings}")
_rc=0
CLAUDE_HOME="${_tmp}" python3 "${HELPER}" >/dev/null 2>&1 || _rc=$? # popup-safe-env-suppressed
_hash_after=$(file_hash "${_settings}")
if [[ ${_rc} -eq 0 ]]; then
    fail "${C}" "expected non-zero exit, got 0"
elif [[ "${_hash_before}" != "${_hash_after}" ]]; then
    fail "${C}" "file was modified despite wrong-typed skillOverrides"
else
    pass "${C}"
fi
rm -rf "${_tmp}"

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
_total=$(( PASS + FAIL ))
echo ""
if [[ ${FAIL} -eq 0 ]]; then
    echo "ALL PASS (${PASS}/${_total})"
    exit 0
else
    echo "FAILED ${FAIL}/${_total}"
    exit 1
fi
