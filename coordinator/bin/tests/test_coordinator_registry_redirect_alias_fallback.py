"""The redirectAliases fallback literal in coordinator_registry must equal the vendored manifest's list.

Trap: the literal only serves manifests predating redirectAliases, so a drift between the two is silent
until such a manifest is loaded -- content-root-em / coordinator-content-repo-em bounced as publish
mirrors that way (DoE friction-log L59).
"""
import ast
import json
from pathlib import Path

_LIB = Path(__file__).resolve().parents[1] / "lib"


def _fallback_literal() -> list:
    tree = ast.parse((_LIB / "coordinator_registry.py").read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "get"
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and node.args[0].value == "redirectAliases"
        ):
            return ast.literal_eval(node.args[1])
    raise AssertionError("no redirectAliases fallback literal in coordinator_registry.py")


def test_fallback_literal_equals_vendored_manifest_redirect_aliases() -> None:
    manifest = json.loads((_LIB / "_vendor" / "coordinator-registry.manifest.json").read_text(encoding="utf-8"))
    assert _fallback_literal() == manifest["identity"]["redirectAliases"]


def test_content_root_ids_redirect() -> None:
    assert {"content-root-em", "coordinator-content-repo-em"} <= set(_fallback_literal())
