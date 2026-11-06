from __future__ import annotations

import fnmatch
import re

# Terminated, so no token is a prefix of another: restoring PROTECTED-LITERAL-1 must not
# rewrite the head of PROTECTED-LITERAL-14 (that left '<literal-1>4' in published bytes).
_TOKEN_TEMPLATE = 'PROTECTED-LITERAL-{index}-END'

#: `<repo>:` qualifier a store may put on an `exclude_paths` value (e.g.
#: `coordinator-content-repo:coordinator/templates/x`); the published `rel_path` is repo-relative, so
#: the qualifier is dropped before matching.
_REPO_QUALIFIER = re.compile(r'^[A-Za-z0-9_.-]+:(?!/)')


def _norm_path(path: str) -> str:
    return _REPO_QUALIFIER.sub('', path.replace('\\', '/')).removeprefix('./')


def _excluded(rel_path: str, exclude_paths: list[str]) -> bool:
    rel = _norm_path(rel_path)
    for pattern in exclude_paths:
        pat = _norm_path(pattern)
        if pat and (rel == pat or fnmatch.fnmatchcase(rel, pat)):
            return True
    return False


def scope_protected_literals(entries, rel_path: str | None) -> list[str]:
    """Flatten store entries to the literal strings in scope for `rel_path`.

    An entry is a plain string (protects in every file) or `{literal, exclude_paths}`
    (protects everywhere except the listed paths). `rel_path=None` applies no exclusion.
    """
    out: list[str] = []
    for entry in entries or ():
        if isinstance(entry, str):
            out.append(entry)
        elif rel_path is None or not _excluded(rel_path, entry.get('exclude_paths') or []):
            out.append(entry['literal'])
    return out


def mask_protected_literals(
    text: str, literals, rel_path: str | None = None,
) -> tuple[str, dict[str, str]]:
    literals = scope_protected_literals(literals, rel_path)
    if not literals:
        return text, {}
    ordered = sorted(dict.fromkeys(literals), key=len, reverse=True)
    restore_map: dict[str, str] = {}
    for index, literal in enumerate(ordered):
        if not literal or literal not in text:
            continue
        token = _TOKEN_TEMPLATE.format(index=index)
        text = text.replace(literal, token)
        restore_map[token] = literal
    return text, restore_map


def restore_protected_literals(text: str, restore_map: dict[str, str]) -> str:
    for token, literal in restore_map.items():
        text = text.replace(token, literal)
    return text
