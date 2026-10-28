"""UE engine-facts clause for composed briefs.

`ue_engine_facts_clause(repo_root)` returns the clause when example-retrieval-repo serves the ue-addon
source for the repo, else "". No subprocess, no socket; detection is delegated to the addon's
own detector (never a second marker list here, never a recursive `.uproject` search).
"""

from __future__ import annotations

import importlib.util
import logging
from pathlib import Path

from coordinator_core.hooks.example_retrieval_repo_detect import find_example_retrieval_repo_dir

_LOG = logging.getLogger(__name__)

_ADDON_MODULE = "project_rag_ue_addon"

CLAUSE_STALE_LINE = (
    "UE project: your training data on UE5 APIs is stale. Before writing or asserting UE API, "
    "call project_engine_session_primer once, then project_cpp_symbol / project_semantic_search; "
    "validate with validate_ue_api."
)
CLAUSE_ENUMERATION_LINE = (
    'For "what exists" questions use project_engine_list_modules / the listing tools, not '
    "semantic search; example_retrieval_repo_instructions() has the full tool map."
)
CLAUSE = CLAUSE_STALE_LINE + "\n" + CLAUSE_ENUMERATION_LINE


def ue_engine_facts_clause(repo_root: str) -> str:
    """Clause text when example-retrieval-repo serves the ue-addon for `repo_root`, else ""; never raises."""
    try:
        rag_dir = find_example_retrieval_repo_dir(repo_root)
        if rag_dir is None:
            return ""
        if importlib.util.find_spec(_ADDON_MODULE) is None:
            return ""
        from example_retrieval_repo_ue_addon.tooling_opt_in import ue_tooling_enabled

        return CLAUSE if ue_tooling_enabled(Path(repo_root)) else ""
    except Exception:
        _LOG.debug("ue engine-facts detection failed; treating as not served", exc_info=True)
        return ""
