"""Named-persona review mode, executed only by the detached runner worker."""
from pathlib import Path

from coordinator_core.agent_pipe.reviewer_provider import ProviderChoice
from coordinator_core.agent_pipe.reviewer_run import FAILED, run_reviewer
from coordinator_core.codex_runner.contract import OnSpawn, RunRequest, RunResult, STATUS_FAILED, STATUS_OK


def run(req: RunRequest, run_dir: Path, on_spawn: OnSpawn) -> RunResult:
    """Run one provider review and retain the persona's ordinary verdict/findings shape."""
    result = run_reviewer(
        req.agent_type, req.brief, ProviderChoice(req.provider, req.model),
        verdicts=req.verdicts, on_spawn=on_spawn, cwd=req.repo_root,
    )
    return RunResult(
        run_id=run_dir.name, status=STATUS_FAILED if result["verdict"] == FAILED else STATUS_OK,
        verdict=result["verdict"], findings=result.get("findings", []), error=result.get("error"),
        usage={"provider": req.provider, "model": req.model},
    )
