"""Contract for ``app_session.validate``: config parse, route result, defect rule,
browser-driver protocol, report shape, and the detached-runner seam.

Stdlib only; never imports Playwright. ``parse_validate_config`` is pure. Env
VALUES never leave the process through any serialiser here: ``report_dict`` and
``RunSpec`` carry env keys only, and the runner re-resolves values from
coordinator.local.md.
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Protocol, Tuple, Union

from coordinator_core.ops._app_session_runtime import win_safe_shlex_split

WIDTHS: Tuple[Tuple[int, int], ...] = ((1440, 900), (390, 844))

RUN_MODULE = "coordinator_core.ops._app_session_validate_run"
RESULT_FILE = "result.json"
SPEC_FILE = "spec.json"

REPORT_SCHEMA = "app-session-validate-report"
REPORT_VERSION = 1

_DEFAULT_READY_PATH = "/"
_DEFAULT_READY_TIMEOUT_S = 120
_DEFAULT_BUILD_TIMEOUT_S = 900
_PER_ROUTE_WIDTH_BUDGET_S = 60


@dataclass(frozen=True)
class Route:
    name: str
    path: str
    expect_status: Optional[int] = None


@dataclass(frozen=True)
class ConfigError:
    field: str
    reason: str


@dataclass(frozen=True)
class ValidateConfig:
    build: Optional[List[str]]
    start: Optional[List[str]]
    port: int
    routes: Tuple[Route, ...]
    ready_path: str = _DEFAULT_READY_PATH
    ready_timeout_s: int = _DEFAULT_READY_TIMEOUT_S
    build_timeout_s: int = _DEFAULT_BUILD_TIMEOUT_S
    build_isolated: bool = False
    env: Dict[str, str] = field(default_factory=dict)
    request_headers: Dict[str, str] = field(default_factory=dict)
    variant: Optional[str] = None


@dataclass(frozen=True)
class RouteResult:
    route: str
    path: str
    width: int
    status: Optional[int]
    console_errors: List[str]
    failed_responses: List[Dict[str, Any]]
    broken_images: List[str]
    overflow_px: int
    overflow_culprit: Optional[str]
    screenshot: str
    error: Optional[str]


@dataclass(frozen=True)
class BrowserAvailability:
    ok: bool
    reason: Optional[str] = None
    install_hint: Optional[str] = None


class BrowserDriver(Protocol):
    def __enter__(self) -> "BrowserDriver": ...

    def __exit__(self, exc_type, exc, tb) -> Optional[bool]: ...

    def sweep_route(
        self,
        base_url: str,
        route: Route,
        width: int,
        height: int,
        headers: Dict[str, str],
        screenshot_path: Path,
    ) -> RouteResult: ...


_TRUE = {"true", "yes", "on", "1"}
_FALSE = {"false", "no", "off", "0"}
_MISSING = object()


def _as_int(value: Any) -> Optional[int]:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError:
            return None
    return None


def _as_bool(value: Any) -> Optional[bool]:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        low = value.strip().lower()
        if low in _TRUE:
            return True
        if low in _FALSE:
            return False
    return None


def _str_map(value: Any, name: str) -> Union[Dict[str, str], ConfigError]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        return ConfigError(name, "must be a mapping")
    out: Dict[str, str] = {}
    for k, v in value.items():
        if isinstance(v, bool):
            out[str(k)] = "true" if v else "false"
        else:
            out[str(k)] = "" if v is None else str(v)
    return out


def _argv(value: Any, name: str) -> Union[Optional[List[str]], ConfigError]:
    if value is None or value == "":
        return None
    if isinstance(value, list):
        return [str(p) for p in value]
    if not isinstance(value, str):
        return ConfigError(name, "must be a command string")
    try:
        return list(win_safe_shlex_split(value))
    except ValueError as exc:
        return ConfigError(name, f"unparseable command: {exc}")


def _int_field(
    target: Dict[str, Any], name: str, default: Optional[int]
) -> Union[int, ConfigError]:
    raw = target.get(name, _MISSING)
    if raw is _MISSING or raw is None or raw == "":
        if default is None:
            return ConfigError(name, "required")
        return default
    n = _as_int(raw)
    if n is None:
        return ConfigError(name, "must be an integer")
    if n <= 0:
        return ConfigError(name, "must be positive")
    return n


def _parse_routes(raw: Any) -> Union[Tuple[Route, ...], ConfigError]:
    if not isinstance(raw, dict) or not raw:
        return ConfigError("routes", "must be a non-empty mapping")
    routes: List[Route] = []
    for name, spec in raw.items():
        fname = f"routes.{name}"
        expect: Optional[int] = None
        if isinstance(spec, dict):
            path = spec.get("path")
            if "expect_status" in spec and spec["expect_status"] not in (None, ""):
                expect = _as_int(spec["expect_status"])
                if expect is None:
                    return ConfigError(f"{fname}.expect_status", "must be an integer")
        else:
            path = spec
        if not isinstance(path, str) or not path.startswith("/"):
            return ConfigError(fname, "path must start with '/'")
        routes.append(Route(name=str(name), path=path, expect_status=expect))
    return tuple(routes)


def parse_validate_config(
    target: Dict[str, Any], variant: Optional[str] = None
) -> Union[ValidateConfig, ConfigError]:
    """Pure parse of one ``app_session`` target mapping; string leaves are coerced."""
    if not isinstance(target, dict):
        return ConfigError("target", "must be a mapping")

    port = _int_field(target, "port", None)
    if isinstance(port, ConfigError):
        return port
    routes = _parse_routes(target.get("routes"))
    if isinstance(routes, ConfigError):
        return routes
    ready_timeout = _int_field(target, "ready_timeout_s", _DEFAULT_READY_TIMEOUT_S)
    if isinstance(ready_timeout, ConfigError):
        return ready_timeout
    build_timeout = _int_field(target, "build_timeout_s", _DEFAULT_BUILD_TIMEOUT_S)
    if isinstance(build_timeout, ConfigError):
        return build_timeout

    isolated_raw = target.get("build_isolated", False)
    isolated = False if isolated_raw in (None, "") else _as_bool(isolated_raw)
    if isolated is None:
        return ConfigError("build_isolated", "must be a boolean")

    ready_path = target.get("ready_path") or _DEFAULT_READY_PATH
    if not isinstance(ready_path, str) or not ready_path.startswith("/"):
        return ConfigError("ready_path", "must start with '/'")

    build = _argv(target.get("build"), "build")
    if isinstance(build, ConfigError):
        return build
    start = _argv(target.get("start"), "start")
    if isinstance(start, ConfigError):
        return start

    env = _str_map(target.get("env"), "env")
    if isinstance(env, ConfigError):
        return env
    headers = _str_map(target.get("request_headers"), "request_headers")
    if isinstance(headers, ConfigError):
        return headers

    if variant is not None:
        variants = target.get("variants")
        if not isinstance(variants, dict) or variant not in variants:
            return ConfigError("variant", f"unknown variant {variant!r}")
        overlay_src = variants[variant]
        overlay = _str_map(
            overlay_src.get("env") if isinstance(overlay_src, dict) else None,
            f"variants.{variant}.env",
        )
        if isinstance(overlay, ConfigError):
            return overlay
        env = {**env, **overlay}

    return ValidateConfig(
        build=build,
        start=start,
        port=port,
        routes=routes,
        ready_path=ready_path,
        ready_timeout_s=ready_timeout,
        build_timeout_s=build_timeout,
        build_isolated=isolated,
        env=env,
        request_headers=headers,
        variant=variant,
    )


def is_defect(result: RouteResult, route: Route) -> bool:
    """True when a sweep result records any defect class.

    The main document is judged by ``status`` alone, so a declared 404 route is
    clean; drivers never list the main document in ``failed_responses``.
    """
    if result.error:
        return True
    if route.expect_status is None:
        if result.status is not None and result.status >= 400:
            return True
    elif result.status != route.expect_status:
        return True
    return bool(
        result.console_errors
        or result.failed_responses
        or result.broken_images
        or result.overflow_px > 0
    )


def report_dict(
    *,
    key: str,
    variant: Optional[str],
    started_at: str,
    finished_at: str,
    base_url: str,
    build_ran: bool,
    build_exit_code: Optional[int],
    build_log: Optional[str],
    env_keys: List[str],
    results: List[RouteResult],
    defect_count: int,
    screenshots_dir: str,
) -> Dict[str, Any]:
    """The report.json body; ``env_keys`` names keys and never values."""
    return {
        "schema": REPORT_SCHEMA,
        "version": REPORT_VERSION,
        "key": key,
        "variant": variant,
        "started_at": started_at,
        "finished_at": finished_at,
        "base_url": base_url,
        "build": {"ran": build_ran, "exit_code": build_exit_code, "log": build_log},
        "env_keys": sorted(env_keys),
        "results": [asdict(r) for r in results],
        "defect_count": defect_count,
        "screenshots_dir": screenshots_dir,
    }


def runner_argv(run_dir: Path) -> List[str]:
    return [sys.executable, "-m", RUN_MODULE, "--run-dir", str(run_dir)]


def config_spec_dict(config: ValidateConfig) -> Dict[str, Any]:
    """A ``ValidateConfig`` as a JSON dict whose ``env`` is a sorted key list."""
    d = asdict(config)
    d["routes"] = list(d["routes"])
    d["env"] = sorted(config.env)
    return d


@dataclass(frozen=True)
class RunSpec:
    key: str
    variant: Optional[str]
    repo_root: str
    worktree: str
    config: Dict[str, Any]
    lock_path: str
    handle_key: str
    created_at: str


def write_run_spec(run_dir: Path, spec: RunSpec) -> Path:
    """Write ``spec.json`` atomically; refuses a ``config.env`` carrying values."""
    env = spec.config.get("env", [])
    if isinstance(env, dict):
        raise ValueError("RunSpec.config.env must be a key list, not a mapping")
    run_dir = Path(run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)
    path = run_dir / SPEC_FILE
    tmp = run_dir / (SPEC_FILE + ".tmp")
    tmp.write_text(json.dumps(asdict(spec), indent=2), encoding="utf-8")
    os.replace(tmp, path)
    return path


def read_run_spec(run_dir: Path) -> RunSpec:
    data = json.loads((Path(run_dir) / SPEC_FILE).read_text(encoding="utf-8"))
    return RunSpec(**data)


def read_result(run_dir: Path) -> Optional[Dict[str, Any]]:
    """The runner's final result, or None until it exists.

    The writer publishes with ``os.replace``, so a present file is whole; an
    unparseable one is treated as not yet written.
    """
    path = Path(run_dir) / RESULT_FILE
    try:
        text = path.read_text(encoding="utf-8")
    except (FileNotFoundError, OSError):
        return None
    try:
        data = json.loads(text)
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def wait_budget_s(config: ValidateConfig) -> int:
    return (
        config.build_timeout_s
        + config.ready_timeout_s
        + _PER_ROUTE_WIDTH_BUDGET_S * len(config.routes) * len(WIDTHS)
    )
