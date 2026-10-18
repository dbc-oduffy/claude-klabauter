"""Pins emit._AGENT_MODELS to coordinator-content-repo's agent definitions and the override refusal."""
import ast
import os
import re
from pathlib import Path

import pytest

from coordinator_core.content_root import read_content_root
from coordinator_core.ops.dispatch_emit import emit

_MODEL_LINE = re.compile(r"^model:\s*['\"]?([A-Za-z0-9._-]+)['\"]?\s*$", re.MULTILINE)


def _doe_agents_dir():
    candidates = []
    env = os.environ.get("CONTENT_ROOT_ROOT")
    if env:
        candidates.append(Path(env))
    content_root = read_content_root()
    if content_root:
        candidates.append(Path(content_root))
    for root in candidates:
        for agents in (root / "coordinator" / "agents", root / "agents"):
            if agents.is_dir():
                return agents
    return None


def _doe_model(agents_dir, agent_type):
    text = (agents_dir / f"{agent_type.split(':', 1)[1]}.md").read_text(encoding="utf-8")
    front = text.split("---", 2)[1]
    match = _MODEL_LINE.search(front)
    assert match, f"{agent_type}: no model: frontmatter"
    return match.group(1)


def test_table_equals_doe_model_frontmatter():
    agents_dir = _doe_agents_dir()
    if agents_dir is None:
        pytest.skip("no coordinator-content-repo checkout (set CONTENT_ROOT_ROOT or place it beside this repo)")
    actual = {t: _doe_model(agents_dir, t) for t in emit._AGENT_MODELS}
    drift = {t: (m, actual[t]) for t, m in emit._AGENT_MODELS.items() if actual[t] != m}
    assert not drift, f"engine vs DoE model drift: {drift}"


@pytest.mark.parametrize("agent_type", sorted(emit._AGENT_MODELS))
def test_override_changing_a_pinned_model_is_refused(agent_type):
    pinned = emit._AGENT_MODELS[agent_type]
    other = "opus" if pinned != "opus" else "haiku"
    with pytest.raises(emit.MalformedAgentOverrideError):
        emit.resolve_agent_model(agent_type, other)
    with pytest.raises(emit.MalformedAgentOverrideError):
        emit._model_opt(agent_type, other)
    assert emit.resolve_agent_model(agent_type, pinned) == pinned
    assert emit.resolve_agent_model(agent_type) == pinned


_MODEL_NAMES = {"haiku", "sonnet", "opus"}
_UNTYPED = {None, "", "general-purpose"}
#: Module-level model constants of calls that carry no agent type.
_UNTYPED_MODEL_CONSTANTS = {"_MECHANICAL_MODEL"}


def _str_const(node):
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None


def _str_leaves(node):
    """String constants a value can take, walking both branches of a conditional expression."""
    if isinstance(node, ast.IfExp):
        return _str_leaves(node.body) + _str_leaves(node.orelse)
    literal = _str_const(node)
    return [] if literal is None else [literal]


def _is_untyped(node):
    leaves = _str_leaves(node)
    return bool(leaves) and all(v in _UNTYPED for v in leaves) and not isinstance(node, ast.Name)


def _composer_literal_models():
    """Sites in dispatch_emit composers that name a model by literal for a typed agent call."""
    hits = []
    for path in sorted(Path(emit.__file__).parent.glob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Call):
                kws = {k.arg: k.value for k in node.keywords if k.arg}
                literals = _str_leaves(kws["agent_model"]) if "agent_model" in kws else []
                literals += _str_leaves(kws["model"]) if "model" in kws else []
                if not literals or "agent_type" not in kws:
                    continue
                if _is_untyped(kws["agent_type"]):
                    continue
                hits.append(f"{path.name}:{node.lineno} model={literals!r}")
            elif isinstance(node, ast.Assign):
                names = [
                    n.id
                    for t in node.targets
                    for n in (t.elts if isinstance(t, ast.Tuple) else [t])
                    if isinstance(n, ast.Name)
                ]
                values = node.value.elts if isinstance(node.value, ast.Tuple) else [node.value]
                for name in names:
                    if (
                        name.endswith("_MODEL")
                        and name not in _UNTYPED_MODEL_CONSTANTS
                        and any(leaf in _MODEL_NAMES for v in values for leaf in _str_leaves(v))
                    ):
                        hits.append(f"{path.name}:{node.lineno} {name}")
    return hits


def test_no_composer_names_a_literal_model_for_a_typed_call():
    assert not _composer_literal_models()


def _composer_agent_types():
    """``coordinator:`` agent types a composer names via an agent_type keyword or *_AGENT_TYPE constant."""
    found = {}
    for path in sorted(Path(emit.__file__).parent.glob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            values = []
            if isinstance(node, ast.Call):
                values = [k.value for k in node.keywords if k.arg in ("agent_type", "agentType")]
            elif isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id.endswith("AGENT_TYPE") for t in node.targets
            ):
                values = [node.value]
            for value in values:
                for leaf in _str_leaves(value):
                    if leaf.startswith("coordinator:"):
                        found[leaf] = f"{path.name}:{node.lineno}"
    return found


def test_every_composer_agent_type_has_a_table_entry():
    missing = {t: w for t, w in _composer_agent_types().items() if t not in emit._AGENT_MODELS}
    assert not missing


def test_agent_type_scan_catches_an_unlisted_type(tmp_path, monkeypatch):
    (tmp_path / "composer.py").write_text(
        "f(agent_type='coordinator:nope' if x else 'general-purpose')\n", encoding="utf-8"
    )
    monkeypatch.setattr(emit, "__file__", str(tmp_path / "emit.py"))
    assert set(_composer_agent_types()) == {"coordinator:nope"}


def test_literal_scan_walks_conditional_branches(tmp_path, monkeypatch):
    (tmp_path / "composer.py").write_text(
        "f(agent_type='coordinator:apm', agent_model='opus' if x else 'sonnet')\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(emit, "__file__", str(tmp_path / "emit.py"))
    assert len(_composer_literal_models()) == 1


def test_literal_scan_catches_a_typed_literal(tmp_path, monkeypatch):
    src = tmp_path / "composer.py"
    src.write_text(
        "_X_MODEL = 'opus'\n"
        "f(agent_type='coordinator:apm', agent_model='sonnet')\n"
        "f(agent_type='general-purpose', agent_model='sonnet')\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(emit, "__file__", str(tmp_path / "emit.py"))
    assert len(_composer_literal_models()) == 2


def test_unpinned_type_takes_its_override_and_refuses_without_one():
    assert emit.resolve_agent_model("coordinator:workflow-maker", "opus") == "opus"
    with pytest.raises(emit.MalformedAgentOverrideError):
        emit.resolve_agent_model("coordinator:workflow-maker")
