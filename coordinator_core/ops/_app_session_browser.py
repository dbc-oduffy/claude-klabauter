"""Python Playwright driver for ``app_session.validate``: availability probes and
the per-route sweep.

Playwright is imported lazily inside ``PlaywrightDriver.__enter__`` so
``coordinator_core`` imports without the ``browser`` extra. Neither probe
downloads anything; the install hints name ``sys.executable``.

Windows trap: the library starts its Node driver without ``CREATE_NO_WINDOW``;
the detached runner must itself run console-less so the driver inherits a
hidden console.
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

from coordinator_core.ops._app_session_validate_contract import (
    BrowserAvailability,
    Route,
    RouteResult,
)

NAV_TIMEOUT_MS = 30_000
_RSC_ABORT = "net::ERR_ABORTED"
_MAIN_DOC_LOAD_FAILURE = "Failed to load resource"
_BROWSER_DIR_PREFIX = "chromium_headless_shell-"
_BROWSER_JSON_NAME = "chromium-headless-shell"

_BROKEN_IMAGES_JS = (
    "() => Array.from(document.images)"
    ".filter(i => i.complete && i.naturalWidth === 0)"
    ".map(i => i.currentSrc || i.src)"
)

_OVERFLOW_JS = """() => {
  const root = document.documentElement;
  const cw = root.clientWidth;
  const px = Math.max(0, root.scrollWidth - cw);
  if (px <= 0) return {px: 0, culprit: null};
  const path = (el) => {
    const parts = [];
    for (let n = el; n && n.nodeType === 1 && parts.length < 4; n = n.parentElement) {
      let s = n.tagName.toLowerCase();
      if (n.id) { s += '#' + n.id; }
      else if (n.classList && n.classList.length) { s += '.' + n.classList[0]; }
      parts.unshift(s);
    }
    return parts.join(' > ');
  };
  let worst = null, worstRight = cw;
  for (const el of document.body ? document.body.querySelectorAll('*') : []) {
    const r = el.getBoundingClientRect();
    if (r.width > 0 && r.right > worstRight) { worst = el; worstRight = r.right; }
  }
  return {px: px, culprit: worst ? path(worst) : null};
}"""


def _install_hint_package() -> str:
    return f'"{sys.executable}" -m pip install "playwright>=1.64"'


def _install_hint_browser() -> str:
    return f'"{sys.executable}" -m playwright install chromium'


def probe_import() -> BrowserAvailability:
    """Whether Playwright is importable, via ``find_spec`` only."""
    try:
        spec = importlib.util.find_spec("playwright")
    except (ImportError, ValueError):
        spec = None
    if spec is None:
        return BrowserAvailability(
            ok=False,
            reason="playwright is not importable",
            install_hint=_install_hint_package(),
        )
    return BrowserAvailability(ok=True)


def _browsers_path() -> Path:
    override = os.environ.get("PLAYWRIGHT_BROWSERS_PATH")
    if override and override != "0":
        return Path(override)
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(base) / "ms-playwright"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Caches" / "ms-playwright"
    return Path.home() / ".cache" / "ms-playwright"


def _required_revision() -> Optional[str]:
    """The headless-shell revision the installed wheel needs, read without importing it."""
    try:
        spec = importlib.util.find_spec("playwright")
    except (ImportError, ValueError):
        return None
    if spec is None or not spec.origin:
        return None
    path = Path(spec.origin).parent / "driver" / "package" / "browsers.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    for entry in data.get("browsers", []):
        if entry.get("name") == _BROWSER_JSON_NAME:
            rev = entry.get("revision")
            return str(rev) if rev else None
    return None


def probe_browser() -> BrowserAvailability:
    """``probe_import`` plus a filesystem check that the required chromium is installed."""
    avail = probe_import()
    if not avail.ok:
        return avail
    root = _browsers_path()
    rev = _required_revision()
    if rev is not None:
        installed = (root / f"{_BROWSER_DIR_PREFIX}{rev}").is_dir()
    else:
        installed = root.is_dir() and any(root.glob(f"{_BROWSER_DIR_PREFIX}*"))
    if not installed:
        return BrowserAvailability(
            ok=False,
            reason=f"chromium is not installed under {root}",
            install_hint=_install_hint_browser(),
        )
    return BrowserAvailability(ok=True)


def _is_rsc_abort(url: str, failure: Optional[str]) -> bool:
    return bool(failure) and _RSC_ABORT in failure and "_rsc=" in url


class PlaywrightDriver:
    """Context manager over one ``sync_playwright`` and one headless chromium."""

    def __init__(self, nav_timeout_ms: int = NAV_TIMEOUT_MS) -> None:
        self._nav_timeout_ms = nav_timeout_ms
        self._pw: Any = None
        self._browser: Any = None

    def __enter__(self) -> "PlaywrightDriver":
        from playwright.sync_api import sync_playwright

        # start() returns the Playwright object; stop() lives on it, not on the manager.
        self._pw = sync_playwright().start()
        try:
            self._browser = self._pw.chromium.launch(headless=True)
        except BaseException:
            self._pw.stop()
            self._pw = None
            raise
        return self

    def __exit__(self, exc_type, exc, tb) -> Optional[bool]:
        try:
            if self._browser is not None:
                self._browser.close()
        finally:
            self._browser = None
            if self._pw is not None:
                self._pw.stop()
            self._pw = None
        return None

    def sweep_route(
        self,
        base_url: str,
        route: Route,
        width: int,
        height: int,
        headers: Dict[str, str],
        screenshot_path: Path,
    ) -> RouteResult:
        """One route at one width in a fresh context.

        A ``goto`` failure (timeout, net error) returns a ``RouteResult`` with ``error``
        set; post-load ``evaluate``/``screenshot`` failures propagate to the caller.
        """
        url = base_url.rstrip("/") + route.path
        context = self._browser.new_context(
            viewport={"width": width, "height": height},
            extra_http_headers=dict(headers),
            device_scale_factor=1,
        )
        console_errors: List[tuple] = []
        page_errors: List[str] = []
        responses: List[Any] = []
        failed_requests: List[Dict[str, Any]] = []
        try:
            page = context.new_page()
            page.on(
                "console",
                lambda m: console_errors.append((m.text, _msg_url(m)))
                if m.type == "error"
                else None,
            )
            page.on("pageerror", lambda e: page_errors.append(str(e)))
            page.on("response", lambda r: responses.append(r))

            def _on_failed(req: Any) -> None:
                failure = req.failure
                if _is_rsc_abort(req.url, failure):
                    return
                failed_requests.append({"url": req.url, "failure": failure})

            page.on("requestfailed", _on_failed)

            try:
                main = page.goto(url, wait_until="load", timeout=self._nav_timeout_ms)
            except Exception as exc:  # navigation timeout / net error is a result, not a raise
                return RouteResult(
                    route=route.name,
                    path=route.path,
                    width=width,
                    status=None,
                    console_errors=[t for t, _ in console_errors] + page_errors,
                    failed_responses=[],
                    broken_images=[],
                    overflow_px=0,
                    overflow_culprit=None,
                    screenshot="",
                    error=str(exc).splitlines()[0] if str(exc) else type(exc).__name__,
                )

            status = main.status if main is not None else None
            main_url = main.url if main is not None else url
            failed_responses: List[Dict[str, Any]] = []
            for resp in responses:
                if resp is main or resp.status < 400:
                    continue
                failed_responses.append({"url": resp.url, "status": resp.status})
            failed_responses.extend(failed_requests)

            errors = [
                text
                for text, murl in console_errors
                if not (
                    status is not None
                    and status >= 400
                    and text.startswith(_MAIN_DOC_LOAD_FAILURE)
                    and murl == main_url
                )
            ]
            errors.extend(page_errors)

            broken = list(page.evaluate(_BROKEN_IMAGES_JS) or [])
            overflow = page.evaluate(_OVERFLOW_JS) or {}
            screenshot_path = Path(screenshot_path)
            screenshot_path.parent.mkdir(parents=True, exist_ok=True)
            page.screenshot(path=str(screenshot_path), full_page=True)
            return RouteResult(
                route=route.name,
                path=route.path,
                width=width,
                status=status,
                console_errors=errors,
                failed_responses=failed_responses,
                broken_images=broken,
                overflow_px=int(overflow.get("px") or 0),
                overflow_culprit=overflow.get("culprit"),
                screenshot=str(screenshot_path),
                error=None,
            )
        finally:
            try:
                context.close()
            except Exception:
                pass


def _msg_url(msg: Any) -> Optional[str]:
    try:
        loc = msg.location
    except Exception:
        return None
    return loc.get("url") if isinstance(loc, dict) else None
