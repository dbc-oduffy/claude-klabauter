"""Corpus equivalence and timing harness for the shared YAML loader.

Purpose: validate every tracked record through the frontmatter validators once with
`yaml.SafeLoader` and once with the shared `yaml_load.SAFE_LOADER`, compare the results
byte for byte, and print `diff_count` plus process time per door per loader.

Run: `python coordinator_core/benchmarks/yaml_loader_equivalence.py [--limit N] [--door NAME]`.
The corpus is `git ls-files '*.md'` under the repo root this file lives in. Not a hot path.

Loader swap: a validator that still calls `yaml.safe_load` is steered by patching
`yaml.safe_load`; one that calls `yaml_load.safe_load` is steered by patching
`yaml_load.SAFE_LOADER` (read at call time). Both are patched together, so the same run is
valid before and after the validators switch.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import subprocess
import sys
import time
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import yaml  # noqa: E402

from coordinator_core.frontmatter import primitives, schema_validate, yaml_load  # noqa: E402

_REAL_SAFE_LOAD = yaml.safe_load
_SHARED_LOADER = yaml_load.SAFE_LOADER

_PLAN_MARKER = "plan-tasks"

_MALFORMED_SPINES = {
    "bad-indent": "- id: C1\n  title: x\n   surface: y\n",
    "unclosed-flow-mapping": "- id: C1\n  title: {a: 1\n  surface: y\n",
    "tab-indent": "- id: C1\n\ttitle: x\n",
    "unclosed-flow-sequence": "- id: C1\n  writes: [a, b\n  surface: y\n",
    "unclosed-quote": "- id: C1\n  title: \"never closed\n  surface: y\n",
    "duplicate-anchor-alias": "- id: C1\n  title: *missing\n",
    "colon-in-plain-scalar": "- id: C1\n  title: a: b: c\n",
    "stray-block-end": "- id: C1\n  title: x\n- - -\n  - y\n: z\n",
    "bad-directive": "%YAML 9.9\n---\n- id: C1\n",
    "control-char": "- id: C1\n  title: \x07bell\n",
}


def _spine_source(spine: str) -> str:
    return "---\ntitle: t\n---\n\n## Tasks\n\n```yaml plan-tasks\n" + spine + "```\n"


@contextlib.contextmanager
def _loader_patched(loader: type):
    yaml_load.SAFE_LOADER = loader
    yaml.safe_load = yaml_load.safe_load
    try:
        yield
    finally:
        yaml.safe_load = _REAL_SAFE_LOAD
        yaml_load.SAFE_LOADER = _SHARED_LOADER


def _frontmatter_door(path: str, text: str):
    parsed = schema_validate.parse_frontmatter(text)
    fm = parsed["frontmatter"]
    if fm is None:
        return parsed
    schemas = _frontmatter_door.schemas
    match = schema_validate.match_schema(path, fm, schemas)
    verdict = schema_validate.validate_frontmatter_obj(fm, match["schema"]) if match else None
    return parsed, verdict


def _plan_rows_door(path: str, text: str):
    return schema_validate._plan_tasks_spine_rows(text)


def _plan_integrity_door(path: str, text: str):
    return schema_validate.plan_tasks_spine_integrity(text)


def _plan_source_door(path: str, text: str):
    return schema_validate.check_plan_tasks_source(text)


def _approval_sha_door(path: str, text: str):
    return primitives.approval_body_sha(text)


# door name -> (callable, applies to plan files only)
_DOORS = {
    "frontmatter": (_frontmatter_door, False),
    "plan-rows": (_plan_rows_door, True),
    "plan-integrity": (_plan_integrity_door, True),
    "plan-source": (_plan_source_door, True),
    "approval-sha": (_approval_sha_door, True),
}


def _tracked_markdown(root: Path) -> list[str]:
    out = subprocess.run(
        ["git", "-C", str(root), "ls-files", "-z", "--", "*.md"],
        capture_output=True,
        check=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    ).stdout
    return sorted(p for p in out.decode("utf-8").split("\0") if p)


def _digest(call, path: str, text: str) -> str:
    try:
        result = repr(call(path, text))
    except Exception as exc:  # noqa: BLE001 - the exception itself is part of the answer
        result = f"RAISED {type(exc).__name__}: {exc}"
    return hashlib.sha256(result.encode("utf-8", "backslashreplace")).hexdigest()


def _run_door(name: str, items: list[tuple[str, str]], loader: type) -> tuple[dict[str, str], float]:
    call = _DOORS[name][0]
    digests: dict[str, str] = {}
    with _loader_patched(loader):
        start = time.process_time()
        for path, text in items:
            digests[path] = _digest(call, path, text)
        elapsed = time.process_time() - start
    return digests, elapsed * 1000.0


def _malformed_items() -> list[tuple[str, str]]:
    return [(f"malformed:{name}", _spine_source(body)) for name, body in _MALFORMED_SPINES.items()]


def main(argv: list[str] | None = None) -> int:
    from coordinator_core.benchmarks import declare_benchmark_origin

    declare_benchmark_origin()
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--limit", type=int, default=0, help="first N tracked files only (0 = all)")
    parser.add_argument("--door", choices=sorted(_DOORS), action="append", help="restrict to a door")
    args = parser.parse_args(argv)

    paths = _tracked_markdown(_REPO_ROOT)
    if args.limit:
        paths = paths[: args.limit]
    records: list[tuple[str, str]] = []
    for rel in paths:
        try:
            records.append((rel, (_REPO_ROOT / rel).read_text(encoding="utf-8")))
        except (OSError, UnicodeDecodeError):
            continue
    plans = [(p, t) for p, t in records if _PLAN_MARKER in t]
    malformed = _malformed_items()

    _frontmatter_door.schemas = schema_validate.load_schemas(schema_validate._SCHEMAS_DIR)

    print(f"safe_loader: {yaml.SafeLoader.__name__}")
    print(f"shared_loader: {_SHARED_LOADER.__name__}")
    print(f"corpus: {len(records)} tracked .md, {len(plans)} naming plan-tasks, {len(malformed)} malformed fixtures")

    diff_count = 0
    for name in args.door or sorted(_DOORS):
        _, plan_only = _DOORS[name]
        items = plans if plan_only else records
        results = {}
        for label, loader in (("SafeLoader", yaml.SafeLoader), ("shared", _SHARED_LOADER)):
            results[label] = _run_door(name, items, loader)
            print(f"door={name} loader={label} files={len(items)} process_ms={results[label][1]:.1f}")
        a, b = results["SafeLoader"][0], results["shared"][0]
        for path, _ in items:
            if a[path] != b[path]:
                diff_count += 1
                print(f"diff: door={name} path={path}")

    if not args.door or any(d.startswith("plan-") for d in args.door):
        for door in ("plan-integrity", "plan-rows", "plan-source"):
            a, _ = _run_door(door, malformed, yaml.SafeLoader)
            b, _ = _run_door(door, malformed, _SHARED_LOADER)
            for path in a:
                if a[path] != b[path]:
                    diff_count += 1
                    print(f"diff: door={door} path={path}")

    print(f"diff_count: {diff_count}")
    return 1 if diff_count else 0


if __name__ == "__main__":
    raise SystemExit(main())
