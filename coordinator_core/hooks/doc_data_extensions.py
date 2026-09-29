"""Leaf: file extensions that are documentation or data, never dispatchable code.

Imports nothing, so a denial-class guard can share the set with the advisory
nudge without reaching the fleet record through it.
"""

_DOC_DATA_EXTENSIONS: frozenset[str] = frozenset([
    ".md", ".yaml", ".yml", ".json", ".txt", ".toml",
    ".csv", ".lock", ".cfg", ".ini",
])
