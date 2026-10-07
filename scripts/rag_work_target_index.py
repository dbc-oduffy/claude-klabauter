"""Make example-retrieval-repo ready for a cloud session's work-target repos, and say whether it is.

Usage:
    rag_work_target_index.py [KEYS]   # index; KEYS = "repos.a" or "repos.a,repos.b"
    rag_work_target_index.py --status # print the readiness file; exit 0 only when ready

KEYS defaults to the session focus variable (`cloud_setup.SESSION_FOCUS_ENV`), which is
read here because a session runs this with its own environment. Each key resolves to a
mounted checkout by the fleet `repos.<name>` convention.

Per target: register the root with example-retrieval-repo, land the repo's published index bundle,
and run the structural (Layer 1) pass only when no bundle lands. Boot's own bundle pull
runs before the egress proxy authenticates GitHub (private repos answer 403/404 there),
so this post-boot pull is the one that normally succeeds.
Both verbs sit behind example-retrieval-repo's `write` profile, which a cloud boot does not install,
so the profile is installed first when absent. That is bulk transfer: `cloud_setup.py`
only ever starts this script detached.

Cloud-only: refuses to run off a cloud host, so a local install is never touched.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import cloud_setup  # noqa: E402

STATUS_PATH = Path("/root/rag-work-target-readiness.json")
LOG_PATH = Path("/root/rag-work-target-index.log")
WRITE_PROFILE_MODULES = ("torch", "sentence_transformers", "transformers", "einops")
#: Structural producers that need no SCIP build and no embed model (cloud-fleet-index
#: skill, Step 3 "lite pass").
LITE_PRODUCERS = (
    "structural_index_lite_python,structural_index_treesitter,structural_index_typescript,"
    "doc_links_producer,config_cvar_extractor,cpp_cvar_extractor"
)


def parse_targets(raw: str | None) -> list[str]:
    """`repos.a` or `repos.a,repos.b` -> ordered, de-duplicated keys."""
    seen: list[str] = []
    for part in (raw or "").split(","):
        key = part.strip()
        if key and key not in seen:
            seen.append(key)
    return seen


def resolve_roots(keys: list[str], known: dict[str, str] | None = None) -> dict[str, str | None]:
    """Each key's checkout path, or None when nothing mounted answers to it."""
    mounted = {k: str(v) for k, v in cloud_setup.mounted_sibling_checkouts().items()}
    mounted.update(known or {})
    return {key: mounted.get(key) for key in keys}


def read_status(path: Path | None = None) -> dict | None:
    try:
        return json.loads((path or STATUS_PATH).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def write_status(status: dict, path: Path | None = None) -> None:
    path = path or STATUS_PATH
    status["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    targets = status.get("targets") or {}
    states = {t.get("state") for t in targets.values()}
    if not targets:
        status["verdict"] = "no_targets"
    elif states == {"ready"}:
        status["verdict"] = "ready"
    elif states & {"queued", "indexing"} or status.get("phase") == "write_profile":
        status["verdict"] = "indexing"
    else:
        status["verdict"] = "not_ready"
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(status, indent=2), encoding="utf-8", newline="\n")
    tmp.replace(path)


def initial_status(roots: dict[str, str | None], rag_root: str) -> dict:
    return {
        "rag_root": rag_root,
        "log": str(LOG_PATH),
        "remedy": f"{sys.executable} {Path(__file__).resolve()} <repos.key[,repos.key]>",
        "targets": {
            key: {"root": root, "state": "queued" if root else "unresolved"}
            for key, root in roots.items()
        },
    }


#: Ceiling for one logged step (bundle pull, pip install of the write profile); a hung egress
#: proxy otherwise leaves the worker status at "indexing" forever. Probes use PROBE_TIMEOUT_S.
RUN_TIMEOUT_S = 1800.0
PROBE_TIMEOUT_S = 30.0
_TIMEOUT_RC = 124


def _run(argv: list[str], log) -> int:
    log.write(f"\n$ {' '.join(argv)}\n".encode())
    log.flush()
    try:
        return subprocess.run(argv, stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, timeout=RUN_TIMEOUT_S).returncode  # popup-intentional-last-resort: cloud-only script
    except subprocess.TimeoutExpired:
        log.write(f"timed out after {RUN_TIMEOUT_S:.0f}s\n".encode())
        log.flush()
        return _TIMEOUT_RC


#: Attempts at the post-boot bundle pull, and the wait between them: GitHub auth at the
#: egress proxy can still be arriving when this worker starts.
BUNDLE_PULL_ATTEMPTS = 3
BUNDLE_PULL_RETRY_S = 30.0
#: `pull-repo-bundle` exit code meaning no verifiable generation exists — not retried.
_PULL_NO_BUNDLE = 3


def pull_bundle(cli: str, root: str, log, *, sleep=time.sleep) -> int:
    """Land `root`'s published index bundle; 0 when an index is in place after it.

    Retries only a generic failure (exit 1: network/auth), never a precondition refusal
    or an absent bundle.
    """
    owner_repo = cloud_setup._github_owner_repo(Path(root))
    if not owner_repo:
        return _PULL_NO_BUNDLE
    argv = [sys.executable, cli, "pull-repo-bundle", "--project-root", root,
            "--repo", owner_repo, "--no-scratch"]
    rc = 1
    for attempt in range(BUNDLE_PULL_ATTEMPTS):
        if attempt:
            sleep(BUNDLE_PULL_RETRY_S)
        rc = _run(argv, log)
        if rc != 1:
            break
    return rc


def _write_profile_missing() -> bool:
    probe = "import importlib.util,sys;sys.exit(any(importlib.util.find_spec(m) is None for m in sys.argv[1:]))"
    try:
        return subprocess.run([sys.executable, "-c", probe, *WRITE_PROFILE_MODULES], timeout=PROBE_TIMEOUT_S).returncode != 0  # popup-intentional-last-resort: cloud-only script
    except subprocess.TimeoutExpired:
        return True


def _log_probe_failure(msg: str) -> None:
    try:
        with open(LOG_PATH, "ab") as log:
            log.write((msg + "\n").encode())
    except OSError:
        pass


def detect_kind(rag_root: str, root: str) -> str:
    """example-retrieval-repo's own kind detection for `root`; `index --non-interactive` refuses any other."""
    probe = "import sys;from pathlib import Path;from example_retrieval_repo_core.project_type import detect;print(detect(Path(sys.argv[1])))"
    try:
        out = subprocess.run([sys.executable, "-c", probe, root], cwd=rag_root, capture_output=True, text=True, timeout=PROBE_TIMEOUT_S)  # popup-intentional-last-resort: cloud-only script
    except subprocess.TimeoutExpired:
        _log_probe_failure(f"detect_kind timed out for {root}")
        return "generic"
    if out.returncode != 0:
        _log_probe_failure(f"detect_kind failed for {root}: {out.stderr.strip()[-500:]}")
        return "generic"
    return out.stdout.strip()


def run(keys: list[str], rag_root: str, known: dict[str, str] | None = None) -> dict:
    roots = resolve_roots(keys, known)
    status = initial_status(roots, rag_root)
    status["pid"] = os.getpid()
    write_status(status)
    cli = str(Path(rag_root) / f"{cloud_setup.RETRIEVAL_MODULE_PREFIX}_cli.py")
    with open(LOG_PATH, "ab") as log:
        if _write_profile_missing():
            status["phase"] = "write_profile"
            write_status(status)
            started = time.monotonic()
            rc = _run(
                [sys.executable, "-m", "pip", "install", "-q", "-e", f"{rag_root}[write]",
                 "-c", str(Path(rag_root) / "example_retrieval_repo_scripts" / "constraints.txt"),
                 "--extra-index-url", "https://download.pytorch.org/whl/cpu"],
                log,
            )
            status["write_profile"] = {"rc": rc, "elapsed_s": round(time.monotonic() - started, 1)}
        status["phase"] = "index"
        for key, entry in status["targets"].items():
            if entry["state"] != "queued":
                continue
            if status.get("write_profile", {}).get("rc", 0) != 0:
                entry["state"] = "failed"
                entry["reason"] = "write profile install failed; see log"
                continue
            entry["state"] = "indexing"
            write_status(status)
            started = time.monotonic()
            entry["kind"] = detect_kind(rag_root, entry["root"])
            base = [sys.executable, cli, "index", "--project-root", entry["root"], "--kind", entry["kind"]]
            rc = _run([*base, "--register-only"], log)
            if rc == 0 and pull_bundle(cli, entry["root"], log) == 0:
                entry["source"] = "bundle"
            elif rc == 0:
                entry["source"] = "scratch"
                rc = _run([*base, "--require-uproject", "false", "--non-interactive",
                           f"--producers={LITE_PRODUCERS}"], log)
            entry.update(state="ready" if rc == 0 else "failed", rc=rc,
                         elapsed_s=round(time.monotonic() - started, 1))
            write_status(status)
        status["phase"] = "done"
        write_status(status)
    return status


def main(argv: list[str]) -> int:
    if argv[:1] == ["--status"]:
        status = read_status()
        print(json.dumps(status, indent=2) if status else f"no readiness file at {STATUS_PATH}")
        return 0 if status and status.get("verdict") == "ready" else 1
    ok, reason = cloud_setup.host_precondition_met()
    if not ok:
        print(f"[rag_work_target_index] refusing: {reason}")
        return 2
    keys = parse_targets(argv[0] if argv else os.environ.get(cloud_setup.SESSION_FOCUS_ENV))
    rag_root = cloud_setup.locate_existing_checkout(cloud_setup.RETRIEVAL_REPO_SLUG)
    if not keys or rag_root is None:
        print("[rag_work_target_index] nothing to do: no target keys, or no example-retrieval-repo checkout")
        return 2
    status = run(keys, str(rag_root))
    print(json.dumps(status, indent=2))
    return 0 if status["verdict"] == "ready" else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
