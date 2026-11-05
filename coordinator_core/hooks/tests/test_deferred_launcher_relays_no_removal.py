"""No relay prompt a deferred or direct-start launcher script hands a subagent carries a recursive or forced rm."""

from __future__ import annotations

import re

from coordinator_core.hooks import submit_engine_ask as mod

_RM = re.compile(r"\brm\s+(-[a-zA-Z]*[rRf]|--(force|recursive))")


def test_deferred_script_text_relays_no_rm_flags(tmp_path):
    script = mod._write_deferred_script(tmp_path, {"ask": "x", "session_id": ""})
    text = script.read_text(encoding="utf-8")
    assert "--params-file" in text
    assert not _RM.search(text)


def test_every_launcher_template_relays_no_rm_flags():
    templates = {n: v for n, v in vars(mod).items() if n.endswith("_JS") and isinstance(v, str)}
    assert "_DEFERRED_JS" in templates
    for name, body in templates.items():
        assert not _RM.search(body), name


def test_the_guard_pattern_still_matches_what_it_forbids():
    assert _RM.search('rm -f "/x/y.json"') and _RM.search("rm -rf d")
    assert not _RM.search("rm /x/y.json")
