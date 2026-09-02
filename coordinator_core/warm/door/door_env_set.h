/* DO NOT EDIT — generated from coordinator_core/warm/env_forwarding.py
 * Regenerate via coordinator_core/warm/tests/test_env_forwarding_set.py
 * (or the same generator that test imports) whenever FORWARDING_SET
 * changes. A hand-edited copy of this file will be overwritten and
 * will fail the byte-pin test the moment it drifts. */
#ifndef COORDINATOR_WARM_DOOR_ENV_SET_H
#define COORDINATOR_WARM_DOOR_ENV_SET_H

/* X-macro list of forwarded env-var names. Each consumer defines
 * its own X(name) before including this file and #undefs it after:
 *
 *   #define X(name) L"" #name,
 *   static const wchar_t *const kDoorEnvSet[] = { DOOR_ENV_SET(X) };
 *   #undef X
 *
 * door_posix.c's X expands to a plain char* literal instead. See
 * those files for the actual expansion each leg uses. */
#define DOOR_ENV_SET(X) \
    X(COORDINATOR_SETTINGS_HOME) \
    X(COORDINATOR_SESSION_ID) \
    X(CLAUDE_SESSION_ID) \
    X(CLAUDE_CODE_SESSION_ID) \
    X(MACHINE_LOCAL_REGISTRY_DIR) \
    X(CLAUDE_HOME) \
    X(CLAUDE_PLUGIN_ROOT) \
    X(CLAUDE_CONFIG_DIR) \
    X(MACHINE_LOCAL_IMPL) \
    X(COORDINATOR_ROOT) \
    X(DOE_ROOT) \
    X(CLAUDE_PROJECT_DIR)

#endif /* COORDINATOR_WARM_DOOR_ENV_SET_H */
