from __future__ import annotations

import datetime
from typing import Mapping, NamedTuple, Optional

import yaml


class IndexSpecError(ValueError):
    pass


class EntryField(NamedTuple):

    field: str
    label: str


class IndexSpec(NamedTuple):

    index_source_dir: str
    entry_kind: str
    entry_fields: tuple[EntryField, ...]
    index_exclude_when: Optional[Mapping[str, object]]


def coerce_to_string(value: object) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (datetime.date, datetime.datetime)):
        return value.isoformat()
    return str(value)


def _require_str(fm: dict, key: str) -> str:
    v = fm.get(key)
    if not isinstance(v, str) or not v.strip():
        raise IndexSpecError(f"index frontmatter missing required field: {key!r}")
    return v


def parse_index_spec(fm_text: str) -> IndexSpec:
    try:
        fm = yaml.safe_load(fm_text)
    except yaml.YAMLError as e:
        raise IndexSpecError(f"index frontmatter is not valid YAML: {e}") from e
    if not isinstance(fm, dict):
        raise IndexSpecError("index frontmatter did not parse to a mapping")

    index_source_dir = _require_str(fm, "index_source_dir")
    entry_kind = _require_str(fm, "entry_kind")

    raw_fields = fm.get("entry_fields")
    if not isinstance(raw_fields, list) or len(raw_fields) == 0:
        raise IndexSpecError(
            "index frontmatter missing or empty required field: 'entry_fields'"
        )
    entry_fields: list[EntryField] = []
    for i, item in enumerate(raw_fields):
        if not isinstance(item, dict) or "field" not in item or "label" not in item:
            raise IndexSpecError(
                f"entry_fields[{i}] must be a mapping with 'field' and 'label' keys, "
                f"got: {item!r}"
            )
        field = item["field"]
        label = item["label"]
        if not isinstance(field, str) or not field:
            raise IndexSpecError(f"entry_fields[{i}].field must be a non-empty string")
        if not isinstance(label, str) or not label:
            raise IndexSpecError(f"entry_fields[{i}].label must be a non-empty string")
        entry_fields.append(EntryField(field=field, label=label))

    index_exclude_when: Optional[Mapping[str, object]] = None
    if "index_exclude_when" in fm:
        raw_exclude = fm["index_exclude_when"]
        if not isinstance(raw_exclude, dict) or not raw_exclude:
            raise IndexSpecError(
                "index_exclude_when, when present, must be a non-empty mapping of "
                f"field to value, got: {raw_exclude!r}"
            )
        for k in raw_exclude:
            if not isinstance(k, str):
                raise IndexSpecError(
                    f"index_exclude_when key must be a string, got: {k!r}"
                )
        index_exclude_when = raw_exclude

    return IndexSpec(
        index_source_dir=index_source_dir,
        entry_kind=entry_kind,
        entry_fields=tuple(entry_fields),
        index_exclude_when=index_exclude_when,
    )
