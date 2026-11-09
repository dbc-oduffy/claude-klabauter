"""Provider-neutral tier and same-definition persona dispatch contracts.

Model equivalence is deployment configuration, never guessed from model names.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import re

PROVIDERS = frozenset({"anthropic", "openai", "google", "xai", "mistral", "deepseek"})
TIERS = frozenset({"haiku", "sonnet", "opus", "fable", "mythos"})


@dataclass(frozen=True)
class Resolution:
    provider: str
    tier: str
    model: str
    effort: str
    argv: tuple[str, ...]


def resolve(path: Path, provider: str, tier: str) -> Resolution:
    """Read an explicit reviewed model map; missing mappings never fall back."""
    if provider not in PROVIDERS or tier not in TIERS:
        raise ValueError("unknown provider or tier")
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or data.get("version") != 1:
        raise ValueError("tier map requires version 1")
    try:
        row = data["providers"][provider][tier]
    except (KeyError, TypeError) as exc:
        raise ValueError(f"configure reviewed mapping for {provider}/{tier} in {path}") from exc
    if not isinstance(row, dict):
        raise ValueError("tier mapping must be an object")
    model, effort, argv = row.get("model"), row.get("effort"), row.get("argv")
    if not isinstance(model, str) or not model.strip() or not isinstance(effort, str) or not effort.strip():
        raise ValueError("mapping requires nonempty model and effort")
    if not isinstance(argv, list) or not argv or any(not isinstance(x, str) or not x for x in argv):
        raise ValueError("mapping requires nonempty argv list")
    # No shell, command interpolation, or arbitrary format expressions.
    substitutions = {"{model}": model, "{effort}": effort}
    command = tuple(substitutions.get(x, x) for x in argv)
    return Resolution(provider, tier, model, effort, command)


def persona_prompt(path: Path, task: str, tier: str) -> str:
    """Load the same role file used by Claude; personas retain the opus floor."""
    if tier != "opus":
        raise ValueError("named personas require the opus tier")
    text = path.read_text(encoding="utf-8")
    front = re.match(r"\A---\r?\n(.*?)\r?\n---(?:\r?\n|$)", text, re.S)
    if not front:
        raise ValueError("agent definition requires YAML frontmatter")
    model = re.search(r"(?m)^model:\s*['\"]?([^\s'\"]+)", front.group(1))
    if not model or model.group(1) != "opus":
        raise ValueError("named persona definition must declare model: opus")
    if not task.strip():
        raise ValueError("task is required")
    return f"{text}\n\nAssigned task:\n{task}"
