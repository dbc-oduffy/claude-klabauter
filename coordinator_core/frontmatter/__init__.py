"""
coordinator_core.frontmatter

Python port of the YAML frontmatter text-manipulation primitives used by the
Example-doctrine-repo coordinator JS tools. Exposes the unified public surface for import
by C2/C3/C4/C5 executors.
"""
from coordinator_core.frontmatter.primitives import (
    FrontmatterSplit,
    insert_fm_field,
    read_fm_field,
    read_fm_field_unquoted,
    rebuild,
    replace_fm_field,
    replace_fm_field_raw,
    serialize_yaml_scalar,
    split_frontmatter,
    unquote_yaml_scalar,
)

__all__ = [
    "FrontmatterSplit",
    "insert_fm_field",
    "read_fm_field",
    "read_fm_field_unquoted",
    "rebuild",
    "replace_fm_field",
    "replace_fm_field_raw",
    "serialize_yaml_scalar",
    "split_frontmatter",
    "unquote_yaml_scalar",
]
