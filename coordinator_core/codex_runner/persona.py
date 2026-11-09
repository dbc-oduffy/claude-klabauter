"""Registered codex_runner persona mode: shared role definition, configured provider."""
from pathlib import Path

from coordinator_core.codex_runner.contract import RunResult, STATUS_FAILED, STATUS_OK
from coordinator_core.worker_contracts import persona_prompt, resolve
from coordinator_core.worker_contracts.worker import supervise
from coordinator_core.codex_runner.output import read_output


def run(req, run_dir, on_spawn):
    run_dir = Path(run_dir)
    resolved = resolve(Path(req.tier_map), req.provider, "opus")
    prompt = persona_prompt(Path(req.persona), Path(req.task_file).read_text(encoding="utf-8"), "opus")
    output_path = run_dir / "persona.stdout"
    with output_path.open("w", encoding="utf-8") as output:
        code = supervise(Path(req.repo_root), prompt, req.assignment, list(resolved.argv),
                          provider=req.provider, mode="persona", stdout=output, on_spawn=on_spawn)
    text, output_usage = read_output(output_path)
    return RunResult(run_id=run_dir.name, status=STATUS_OK if code == 0 else STATUS_FAILED,
                     output=text, usage=output_usage,
                     error=None if code == 0 else f"provider exited {code}")
