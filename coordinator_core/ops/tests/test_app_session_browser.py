"""Tests for the Playwright driver collectors and probes, over fake page objects."""

from __future__ import annotations

import sys
from types import SimpleNamespace

import pytest

from coordinator_core.ops import _app_session_browser as b
from coordinator_core.ops._app_session_validate_contract import Route


class FakeResponse:
    def __init__(self, url, status):
        self.url = url
        self.status = status


class FakePage:
    def __init__(self, script):
        self.script = script
        self.handlers = {}
        self.shots = []
        self.main = None

    def on(self, name, fn):
        self.handlers[name] = fn

    def goto(self, url, wait_until, timeout):
        assert wait_until == "load"
        self.main = FakeResponse(url, self.script.get("status", 200))
        for kind, payload in self.script.get("events", []):
            if kind == "main":
                self.handlers["response"](self.main)
            else:
                self.handlers[kind](payload)
        if self.script.get("timeout"):
            raise TimeoutError("Page.goto: Timeout 30000ms exceeded.\nCall log: ...")
        return self.main

    def evaluate(self, js):
        if "naturalWidth" in js:
            return self.script.get("broken", [])
        return self.script.get("overflow", {"px": 0, "culprit": None})

    def screenshot(self, path, full_page):
        assert full_page is True
        open(path, "wb").write(b"png")
        self.shots.append(path)


class FakeContext:
    def __init__(self, page):
        self.page = page
        self.closed = False

    def new_page(self):
        return self.page

    def close(self):
        self.closed = True


class FakeBrowser:
    def __init__(self, script):
        self.script = script
        self.kwargs = None
        self.ctx = None

    def new_context(self, **kw):
        self.kwargs = kw
        self.ctx = FakeContext(FakePage(self.script))
        return self.ctx


def _sweep(tmp_path, script, route=None):
    drv = b.PlaywrightDriver()
    drv._browser = FakeBrowser(script)
    res = drv.sweep_route(
        "http://127.0.0.1:3000",
        route or Route("home", "/"),
        390,
        844,
        {"X-A": "1"},
        tmp_path / "shots" / "home-390.png",
    )
    return drv, res


def _console(text, url=None, typ="error"):
    return SimpleNamespace(type=typ, text=text, location={"url": url})


def _failed(url, failure):
    return SimpleNamespace(url=url, failure=failure)


def test_context_options_and_clean_result(tmp_path):
    drv, res = _sweep(tmp_path, {"events": [("main", None)]})
    assert drv._browser.kwargs == {
        "viewport": {"width": 390, "height": 844},
        "extra_http_headers": {"X-A": "1"},
        "device_scale_factor": 1,
    }
    assert res.status == 200 and res.error is None
    assert res.console_errors == [] and res.failed_responses == []
    assert (tmp_path / "shots" / "home-390.png").exists()
    assert drv._browser.ctx.closed


def test_console_and_pageerror_collected_non_errors_ignored(tmp_path):
    ev = [
        ("console", _console("boom")),
        ("console", _console("info", typ="log")),
        ("pageerror", Exception("TypeError: x")),
    ]
    _, res = _sweep(tmp_path, {"events": ev})
    assert res.console_errors == ["boom", "TypeError: x"]


def test_main_document_never_failed_response_and_its_console_line_dropped(tmp_path):
    url = "http://127.0.0.1:3000/missing"
    ev = [
        ("main", None),
        ("console", _console("Failed to load resource: 404", url=url)),
        ("console", _console("real error")),
    ]
    _, res = _sweep(
        tmp_path, {"status": 404, "events": ev}, Route("missing", "/missing", 404)
    )
    assert res.status == 404
    assert res.failed_responses == []
    assert res.console_errors == ["real error"]


def test_subresource_400_is_failed_response_and_200_is_not(tmp_path):
    ev = [
        ("main", None),
        ("response", FakeResponse("http://x/a.js", 500)),
        ("response", FakeResponse("http://x/ok.js", 200)),
    ]
    _, res = _sweep(tmp_path, {"events": ev})
    assert res.failed_responses == [{"url": "http://x/a.js", "status": 500}]


def test_rsc_abort_filtered_other_failures_recorded(tmp_path):
    ev = [
        ("requestfailed", _failed("http://x/p?_rsc=abc", "net::ERR_ABORTED")),
        ("requestfailed", _failed("http://x/p", "net::ERR_ABORTED")),
        ("requestfailed", _failed("http://x/p?_rsc=abc", "net::ERR_CONNECTION_RESET")),
    ]
    _, res = _sweep(tmp_path, {"events": ev})
    assert res.failed_responses == [
        {"url": "http://x/p", "failure": "net::ERR_ABORTED"},
        {"url": "http://x/p?_rsc=abc", "failure": "net::ERR_CONNECTION_RESET"},
    ]


def test_broken_images_and_overflow(tmp_path):
    script = {
        "broken": ["http://x/nope.png"],
        "overflow": {"px": 218, "culprit": "body > div.wide"},
    }
    _, res = _sweep(tmp_path, script)
    assert res.broken_images == ["http://x/nope.png"]
    assert res.overflow_px == 218
    assert res.overflow_culprit == "body > div.wide"


def test_navigation_timeout_becomes_error_not_exception(tmp_path):
    drv, res = _sweep(tmp_path, {"timeout": True})
    assert res.error.startswith("Page.goto: Timeout")
    assert res.status is None and res.screenshot == ""
    assert drv._browser.ctx.closed


def test_probe_import_none_spec_returns_hint(monkeypatch):
    monkeypatch.setattr(b.importlib.util, "find_spec", lambda name: None)
    for probe in (b.probe_import, b.probe_browser):
        avail = probe()
        assert avail.ok is False
        assert sys.executable in avail.install_hint


def test_probe_import_ok_when_spec_present(monkeypatch):
    monkeypatch.setattr(b.importlib.util, "find_spec", lambda name: object())
    assert b.probe_import().ok is True


def test_probe_browser_checks_required_revision_dir(monkeypatch, tmp_path):
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", str(tmp_path))
    monkeypatch.setattr(b.importlib.util, "find_spec", lambda name: object())
    monkeypatch.setattr(b, "_required_revision", lambda: "1248")
    (tmp_path / "chromium_headless_shell-1228").mkdir()
    missing = b.probe_browser()
    assert missing.ok is False
    assert missing.install_hint == f"{sys.executable} -m playwright install chromium"
    (tmp_path / "chromium_headless_shell-1248").mkdir()
    assert b.probe_browser().ok is True


def test_probe_browser_falls_back_to_glob_without_revision(monkeypatch, tmp_path):
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", str(tmp_path))
    monkeypatch.setattr(b.importlib.util, "find_spec", lambda name: object())
    monkeypatch.setattr(b, "_required_revision", lambda: None)
    assert b.probe_browser().ok is False
    (tmp_path / "chromium_headless_shell-9").mkdir()
    assert b.probe_browser().ok is True


def test_module_import_does_not_import_playwright():
    import importlib

    src = importlib.util.find_spec("coordinator_core.ops._app_session_browser")
    text = open(src.origin, encoding="utf-8").read()
    top = text.split("class PlaywrightDriver")[0]
    assert "import playwright" not in top and "from playwright" not in top


class _FakeManager:
    """Like sync_playwright(): start() returns the Playwright object, the manager has no stop()."""

    def __init__(self, pw):
        self._pw = pw

    def start(self):
        return self._pw


def _install_fake_playwright(monkeypatch, launch_raises=False):
    events = []
    browser = SimpleNamespace(close=lambda: events.append("browser.close"))

    def launch(headless):
        if launch_raises:
            raise RuntimeError("launch failed")
        return browser

    pw = SimpleNamespace(
        chromium=SimpleNamespace(launch=launch), stop=lambda: events.append("pw.stop")
    )
    mod = SimpleNamespace(sync_playwright=lambda: _FakeManager(pw))
    monkeypatch.setitem(sys.modules, "playwright", SimpleNamespace(sync_api=mod))
    monkeypatch.setitem(sys.modules, "playwright.sync_api", mod)
    return events


def test_driver_stops_the_started_playwright_object(monkeypatch):
    events = _install_fake_playwright(monkeypatch)
    with b.PlaywrightDriver():
        pass
    assert events == ["browser.close", "pw.stop"]


def test_driver_stops_playwright_when_launch_fails(monkeypatch):
    events = _install_fake_playwright(monkeypatch, launch_raises=True)
    with pytest.raises(RuntimeError):
        with b.PlaywrightDriver():
            pass
    assert events == ["pw.stop"]
