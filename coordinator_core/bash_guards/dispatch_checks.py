"""coordinator_core.bash_guards.dispatch_checks -- re-export facade for the
``check_*`` guards ``dispatch.py`` calls through its ``_dc`` alias. It defines
nothing; every name resolves to the ``_dc_*`` module that owns it.

Shared layer (imported by the family modules, never the reverse):
  _dc_shell_text   segment split, quote strip, heredoc / ``sh -c`` unwrap
  _dc_git_probe    ``_deny``, git-probe budget and deadline, ``_run_git``,
                   owner-sentence text, porcelain dirty-path and peer-claim helpers
  _dc_git_shared   git argv / cwd / cd-chain / hazard-repo / commit-operand helpers

Family layer:
  _dc_git_misc     check_no_verify, check_destructive_git_orphan,
                   check_destructive_git_clean, check_destructive_git_revert
  _dc_rm           check_destructive_rm, check_stale_write
  _dc_blanket_add  check_blanket_git_add
  _dc_commit       check_validate_commit, check_git_commit_safe_commit_advise
  _dc_rewrites     find/grep/sed/heredoc rewrites and advisories, multiprobe banner,
                   check_runaway_find

A test patch on this module reaches no code: patch the ``_dc_*`` module whose
code reads the name.
"""

from __future__ import annotations

import contextvars
import hashlib
import json
import os
import posixpath
import re
import shlex
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Dict, FrozenSet, List, Optional, Set, Tuple

from coordinator_core import machine_path_leak
from coordinator_core.bash_guards import _unborn_scratch_repo, commit_tripwires
from coordinator_core.git.correction_note import SENTINEL as _NOTE_SENTINEL
from coordinator_core.bash_guards._dialect import (
    Dialect,
    _strip_ps_quotes,
    dialect_from_tool_name,
    expand_start_process_invocations,
    resolve_segments_for_dialect,
    tokenize_command,
)
from coordinator_core.bash_guards._helpers import operator_override_note
from coordinator_core.bash_guards._merge_incoming import merge_incoming_paths
from coordinator_core.bash_guards.block_subagent_commit import (
    _literal_pathspec_inner_path as literal_pathspec_inner_path,
    _bracket_candidate_exists_literally as bracket_candidate_exists_literally,
)
from coordinator_core.bash_guards.block_subagent_destructive_action import (
    _PS_REMOVE_VERBS,
    _normalize_executable_basename,
    _normalize_windows_argv0_head_path_with_spaces,
    _ps_normalize_verb_token,
    _ps_resolve_head_verb,
)
from coordinator_core.bash_guards._override_log_path import _override_log_path
from coordinator_core.bash_guards._command_tokenizer import (
    _extract_command_substitutions as _bt_extract_command_substitutions,
    _skip_wrapper_own_argv,
    _strip_heredocs as _bt_strip_heredocs,
    exceeds_tokenizable_ceiling as _bt_exceeds_tokenizable_ceiling,
    segments_from_tokens_simple as _bt_segments_from_tokens_simple,
    segments_from_tokens_with_pipe_flag as _bt_segments_from_tokens_with_pipe_flag,
    split_unquoted_newlines as _bt_split_unquoted_newlines,
    token_matches_binary as _bt_token_matches_binary,
    tokenize_full_command as _bt_tokenize_full_command,
)
from coordinator_core.bash_guards._rewrite_support import (
    _ENV_ASSIGNMENT_TOKEN_RE,
    _GIT_GLOBAL_OPT_NO_ARG_SIMPLE,
    _GIT_GLOBAL_OPT_WITH_ARG,
    _GIT_WRAPPER_PREFIX_WORDS,
    _advisory,
    _allow_rewrite,
    _bt_git_resolved_subcommand,
    _bt_git_subcommand_start_index,
    _bt_peel_wrapper_prefix,
    _bt_python3_invocation,
    _crlf_strip,
    _override,
)
from coordinator_core.bash_guards._shape_classifier import (
    Shape as _BT_Shape,
    classify_command as _bt_classify_command,
)

# Every real write behind these guards (override_log, scope-warnings.log)
# resolves under <git_root>/.git/coordinator-sessions/<session_id>/ or a
# session_dir -- untracked guard bookkeeping, never a tracked repo artifact.
GENERATES = []

# `coordinator_core.session.{liveness,core,scope}` are deliberately NOT
# imported at module scope anywhere in the _dc_* set: an ImportError there
# would take down dispatch.py's whole dispatcher, including every hard-deny
# check. Each is imported lazily at its single call site, degrading to that
# check's own safe fallback.

from coordinator_core.bash_guards import _dc_git_probe, _dc_git_shared
from coordinator_core.bash_guards._dc_shell_text import (
    _BACKTICK_SUBSTITUTION_INTERPRETERS,
    _BT_REDIRECTION_TOKEN_RE,
    _BUNDLED_C_FLAG_RE,
    _HEREDOC_INTRO_RE,
    _HEREDOC_SCRIPTABLE_INTERPRETERS,
    _HEREDOC_SHELL_INTERPRETERS,
    _MAX_HEREDOC_UNWRAP_DEPTH,
    _MAX_SHELL_C_UNWRAP_DEPTH,
    _QUOTE_SENTINEL_DQ,
    _QUOTE_SENTINEL_SQ,
    _RM_WRAPPER_WORDS,
    _SHELL_C_WRAPPER_INTERPRETERS,
    _SPAWN_INDICATOR_TOKENS,
    _bt_has_redirection,
    _bt_is_bare_redirection_token,
    _bt_is_redirection_token,
    _classify_heredoc_intro,
    _heredoc_body_has_spawn_indicator,
    _heredoc_intro_command_word,
    _heredoc_shell_payloads,
    _join_backslash_newlines,
    _line_has_shell_in_command_position,
    _protect_line_quotes,
    _restore_protected_quotes,
    _shell_c_unwrap_payloads,
    _shell_c_unwrap_single_segment,
    _split_segments,
    _strip_heredoc_bodies,
    _strip_heredoc_bodies_for_prose_scan,
    _strip_q,
    _strip_shell_comments_for_prose_scan,
    _strip_ws_quoted_spans,
    _word_present,
)
from coordinator_core.bash_guards._dc_git_probe import (
    _BULK_FOREIGN_INDEX_PATHS,
    _CREATIONFLAGS,
    _EXIT_STATUS_LOAD_BEARING,
    _GIT_PROBE_BUDGET_SECONDS,
    _GIT_PROBE_BUDGET_SPENT_RC,
    _NOTE_CORRECTION,
    _OWNER_NAME_PROVENANCE_WARNING,
    _SAFE_COMMIT_ROUTE_CLAUSE,
    _UNKNOWN_MTIME,
    _arm_git_probe_deadline,
    _batch_show_index_blobs,
    _bulk_foreign_index_refusal,
    _clear_probe_fail_open_reasons,
    _deny,
    _dirty_paths_from_porcelain,
    _disarm_git_probe_deadline,
    _format_batched_scope_denial,
    _format_owner_sentence,
    _format_owner_token,
    _git_probe_budget_spent,
    _git_probe_deadline,
    _git_probe_fail_open_reasons,
    _git_probe_last_failure,
    _name_clause_fits,
    _new_dirty_paths_memo,
    _owner_clause_budget_bytes,
    _owner_display_id,
    _owner_liveness_basis,
    _owner_name_provenance_note,
    _owner_writer_name_clause,
    _persist_probe_fail_open,
    _piped_exit_code_chain,
    _record_probe_fail_open,
    _resolve_owner_writer_name,
    _rm_peer_claim_of,
    _run_git,
    _take_probe_fail_open_reasons,
    _truncate_to_budget,
    _with_name_clause,
)
from coordinator_core.bash_guards._dc_git_shared import (
    _BT_LEADING_CD_RE,
    _BT_PURE_CD_SEGMENT_RE,
    _BT_SEQUENCER_CONTINUE_VERBS,
    _BUNDLED_SHORT_M_RE,
    _BYPASS_PREFIX,
    _COMMIT_BUNDLED_SHORT_RE,
    _FREETEXT_OPERAND_FLAGS,
    _GC_LOADBEARING_PREFIXES,
    _GC_LOADBEARING_RE,
    _GIT_COMMIT_LONG_NO_ARG,
    _GIT_COMMIT_OPT_WITH_ARG,
    _GIT_COMMIT_SHORT_NO_ARG,
    _WRAPPER_FLAG_GROUP,
    _bt_cd_chain_cwd_before,
    _bt_cd_chain_cwds_by_segment,
    _bt_commit_has_explicit_pathspec,
    _bt_commit_has_sweep_all_flag,
    _bt_commit_operand_scan,
    _bt_commit_own_pathspec,
    _bt_git_dash_c_value,
    _bt_git_sequencer_in_progress,
    _bt_is_shattered_operand,
    _bt_leading_cd_prefix_cwd,
    _bt_probe_cwd,
    _extract_git_c_dir,
    _hazard_registry_repo_roots,
    _is_hazard_repo,
    _is_loadbearing,
    _meta_repo_root,
    _new_git_memo,
    _orphan_c_cwd,
    _paths_match,
    _ps_git_bypass_segments,
)
from coordinator_core.bash_guards._dc_git_misc import (
    _BYPASS_FLAG_TOKENS,
    _BYPASS_GPGSIGN_FALSE_RE,
    _BYPASS_HEAD_RE,
    _BYPASS_MARKER_LITERALS,
    _BYPASS_MARKER_RE,
    _BYPASS_RE,
    _BYPASS_WRAPPER_WORDS,
    _BypassRe,
    _CHECK2_SAFE_NONSPAWNING_HEADS,
    _GC_CLEAN_CMD_RE,
    _GIT_EXE_HEAD_TO_BARE_RE,
    _GR_BASE_RE,
    _GR_VERBS,
    _INSPECTS_WITHOUT_EXECING_WORDS,
    _RESET_MODE_FLAGS,
    _after_reset_slice,
    _check_destructive_git_revert_full,
    _command_really_invokes,
    _gc_is_clean_segment,
    _git_argv_subcommand_position,
    _git_reset_invocation,
    _gr_is_revert_segment,
    _local_branch_names,
    _no_verify_rescan_shell_c_and_heredoc,
    _normalize_git_exe_head_to_bare,
    _normalize_windows_git_path_head,
    _orphan_override_hint,
    _reset_ref_moving_mode,
    _seg_confirmed_not_git_invocation,
    _seg_excluding_freetext_operands,
    _seg_forcing_form_scan_text,
    _seg_has_git_bypass_flag,
    _seg_resolved_git_subcommand,
    check_destructive_git_clean,
    check_destructive_git_orphan,
    check_destructive_git_revert,
    check_destructive_git_revert_advisory,
    check_no_verify,
)
from coordinator_core.bash_guards._dc_rm import (
    _HOME_VAR_RE,
    _PS_RECURSE_NAME,
    _RM_CMD_RE,
    _abs_path,
    _attribute_porcelain,
    _expand_home_var,
    _is_same_dir,
    _is_within,
    _ps_recurse_flag,
    _rm_flush_touch,
    _rm_is_rm_segment,
    _stale_write_family_claim,
    _stale_write_shape_candidates,
    check_destructive_rm,
    check_stale_write,
)
from coordinator_core.bash_guards._dc_blanket_add import (
    _GIT_ADD_DASH_C_VALUE_RE,
    _GIT_ADD_GATE_RE,
    _GIT_ADD_GLOBAL_OPT_RE,
    _GIT_ADD_INVOCATION_OPTS_RE,
    _GIT_COMMIT_GATE_RE,
    _add_has_narrowing_pathspec,
    _awk_quote_aware_split,
    _bt_add_resolve_subtree_dir_token,
    _bt_add_subtree_foreign_paths,
    _bt_blanket_add_dash_c_cwd,
    _bt_commit_subtree_foreign_paths,
    _bt_no_peer_can_share_the_index,
    _bt_run_record_paths,
    _bt_run_record_paths_foreign,
    check_blanket_git_add,
)
from coordinator_core.bash_guards._dc_rewrites import (
    _ENV_ASSIGN_RE,
    _FIND_WRAPPER_WORDS,
    _GREP_BASIC_SAFE_ESCAPES,
    _GREP_EXTENDED_SAFE_ESCAPES,
    _GREP_FAMILY_BINARIES_BT,
    _GREP_POSIX_BRACKET_CLASS_RE,
    _GREP_SUBSTITUTABLE_SHORT_FLAGS,
    _HEREDOC_SCRATCH_ROOT_MARKERS,
    _HEREDOC_WRITE_TARGET_RE,
    _NICE_NICENESS_ARG_RE,
    _WRAPPER_WORD_RE,
    _bt_grep_dialect,
    _bt_grep_flags_and_operands,
    _bt_grep_pattern_safe_for_dialect,
    _bt_grep_python_rewrite,
    _bt_grep_translate_pattern,
    _find_is_find_segment,
    _find_is_root_anchor,
    _heredoc_scriptable_bodies,
    _heredoc_target_outside_any_session_repo,
    _heredoc_write_target_is_claude_memory,
    _heredoc_write_target_is_scratch,
    _paths_match_prefix,
    _skip_leading_env_and_wrappers_idx,
    _strip_leading_env_and_wrappers,
    check_cat_heredoc_write_advise,
    check_find_exec_rewrite,
    check_grep_via_bash_rewrite,
    check_heredoc_repo_write_advise,
    check_multiprobe_banner_rewrite,
    check_runaway_find,
    check_sed_range_read_advise,
)
from coordinator_core.bash_guards._dc_commit import (
    _COMMIT_GLOB_META_RE,
    _COMMIT_ONLY_SHORT_RE,
    _GIT_ADD_OPT_WITH_ARG,
    _LESSONS_ARCHIVE_PREFIX,
    _LESSONS_LIVE_PREFIX,
    _SESSION_ID_TRAILER_RE,
    _bt_commit_has_amend_flag,
    _bt_commit_is_noop_invocation,
    _bt_commit_scope_is_sweeping,
    _bt_commit_scope_operand_is_sweeping,
    _bt_commit_sweeping_scope_operands,
    _bt_compound_add_bare_commit,
    _bt_git_index_file_env,
    _bt_head_commit_amend_provenance,
    _bt_solo_bare_commit_index_nonempty,
    _bt_sweep_all_holds_unverifiable_paths,
    _extract_commit_subject,
    _lessons_archive_paired_paths,
    _sweeping_scope_sentence,
    check_git_commit_safe_commit_advise,
    check_validate_commit,
)
