"""Provider registry: resolve a kind to exactly one provider or raise.

No ordering, priority or first-match fallback exists: every qualifying
candidate is collected, and anything other than exactly one is an error.

Consumer: rea ask 2 (the Ghidra adapter).
"""

from __future__ import annotations

from typing import Any, Callable

_PROVIDERS: dict[str, dict[str, Any]] = {}


class ProviderRegistryError(Exception):
    """Base class for provider-registry failures."""


class DuplicateProviderError(ProviderRegistryError):
    """A provider name was registered twice under one kind."""


class NoProviderError(ProviderRegistryError):
    """No registered provider of the kind satisfies the predicate."""


class AmbiguousProviderError(ProviderRegistryError):
    """More than one provider qualifies; `names` lists them sorted."""

    def __init__(self, kind: str, names: list[str]) -> None:
        self.kind = kind
        self.names = names
        super().__init__(f"ambiguous providers for {kind!r}: {', '.join(names)}")


def register_provider(kind: str, name: str, provider: Any) -> None:
    """Register `provider` as `name` under `kind`; a duplicate name raises."""
    by_name = _PROVIDERS.setdefault(kind, {})
    if name in by_name:
        raise DuplicateProviderError(f"provider {name!r} already registered for {kind!r}")
    by_name[name] = provider


def resolve(kind: str, predicate: Callable[[Any], bool]) -> Any:
    """Return the single provider of `kind` satisfying `predicate`.

    Zero qualifying raises NoProviderError; more than one raises
    AmbiguousProviderError with the sorted names.
    """
    hits = [(n, p) for n, p in _PROVIDERS.get(kind, {}).items() if predicate(p)]
    if not hits:
        raise NoProviderError(f"no provider for {kind!r} satisfies the predicate")
    if len(hits) > 1:
        raise AmbiguousProviderError(kind, sorted(n for n, _ in hits))
    return hits[0][1]
