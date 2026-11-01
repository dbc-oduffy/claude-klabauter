"""`yaml_load`: the C loader and its pure-Python fallback answer identically."""

from __future__ import annotations

import datetime
import importlib

import pytest
import yaml

from coordinator_core.frontmatter import yaml_load

_HAS_LIBYAML = hasattr(yaml, "CSafeLoader")

_VALID = {
    "bare date": "when: 2026-11-01\n",
    "datetime": "at: 2026-11-01T18:07:16Z\n",
    "flow mapping": "m: {a: 1, b: [x, y], c: {d: 2026-01-02}}\n",
    "quoted keys": "'quoted key': 1\n\"dq key\": 2\n",
    "block list of maps": "- id: C1\n  writes:\n    - a.py\n  note: |\n    text\n- id: C2\n",
    "scalars": "a: 1\nb: 1.5\nc: yes\nd: null\ne: ~\nf: '007'\ng: 0o17\n",
    "empty": "",
    "anchor": "a: &x [1, 2]\nb: *x\n",
}

_MALFORMED = {
    "bad indent": "- id: C1\n  title: x\n   surface: y\n",
    "unclosed flow mapping": "- id: C1\n  title: {a: 1\n  surface: y\n",
    "tab indent": "- id: C1\n\ttitle: x\n",
    "undefined alias": "- id: C1\n  title: *missing\n",
    "control character": "- id: C1\n  title: \x07bell\n",
}


def _reference(text: str):
    return yaml.load(text, Loader=yaml.SafeLoader)


@pytest.fixture()
def fallback_only(monkeypatch):
    """`yaml_load` imported as if PyYAML had no libyaml; the real module restored after."""
    monkeypatch.delattr(yaml, "CSafeLoader", raising=False)
    module = importlib.reload(yaml_load)
    yield module
    monkeypatch.undo()
    importlib.reload(yaml_load)


def test_shared_loader_is_the_c_loader_when_libyaml_is_present():
    expected = yaml.CSafeLoader if _HAS_LIBYAML else yaml.SafeLoader
    assert yaml_load.SAFE_LOADER is expected


def test_without_libyaml_the_loader_is_the_pure_python_one(fallback_only):
    assert fallback_only.SAFE_LOADER is yaml.SafeLoader


@pytest.mark.parametrize("name", sorted(_VALID))
def test_fallback_object_equals_reference(name, fallback_only):
    text = _VALID[name]
    assert fallback_only.safe_load(text) == _reference(text)


@pytest.mark.parametrize("name", sorted(_VALID))
def test_c_path_object_equals_reference_down_to_types(name):
    text = _VALID[name]
    assert repr(yaml_load.safe_load(text)) == repr(_reference(text))


def test_bare_date_stays_a_date_not_a_string():
    loaded = yaml_load.safe_load(_VALID["bare date"])
    assert loaded == {"when": datetime.date(2026, 11, 1)}


@pytest.mark.parametrize("name", sorted(_MALFORMED))
def test_malformed_text_raises_yaml_error_with_the_reference_message(name):
    text = _MALFORMED[name]
    with pytest.raises(yaml.YAMLError) as expected:
        _reference(text)
    with pytest.raises(yaml.YAMLError) as actual:
        yaml_load.safe_load(text)
    assert type(actual.value) is type(expected.value)
    assert str(actual.value) == str(expected.value)


@pytest.mark.parametrize("name", sorted(_MALFORMED))
def test_malformed_text_raises_in_fallback_too(name, fallback_only):
    with pytest.raises(yaml.YAMLError):
        fallback_only.safe_load(_MALFORMED[name])


@pytest.mark.skipif(not _HAS_LIBYAML, reason="libyaml absent: nothing differs to pin")
def test_raw_c_loader_words_errors_differently_so_the_reparse_is_needed():
    text = _MALFORMED["unclosed flow mapping"]
    with pytest.raises(yaml.YAMLError) as raw:
        yaml.load(text, Loader=yaml.CSafeLoader)
    with pytest.raises(yaml.YAMLError) as reference:
        _reference(text)
    assert str(raw.value) != str(reference.value)


def test_safe_load_reads_the_loader_at_call_time(monkeypatch):
    seen = []

    class Spy(yaml.SafeLoader):
        def __init__(self, stream):
            seen.append(stream)
            super().__init__(stream)

    monkeypatch.setattr(yaml_load, "SAFE_LOADER", Spy)
    assert yaml_load.safe_load("a: 1\n") == {"a": 1}
    assert seen == ["a: 1\n"]
