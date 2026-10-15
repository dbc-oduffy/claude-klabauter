"""load_manifest binds ${CLAUDE_PLUGIN_ROOT} in every loaded template to the content root."""

from __future__ import annotations

import hashlib

import yaml

from coordinator_core.ops.dispatch_emit.pipeline_manifest import load_manifest

TOKEN = "${CLAUDE_PLUGIN_ROOT}"


def _write(tmp_path, templates):
    root = tmp_path / "content" / "pipelines" / "dr"
    root.mkdir(parents=True)
    stages = [
        {"id": f"s{i}", "agent_type": "coordinator:research-scout", "model": "haiku",
         "template": name, "output": f"{{{{scratch_dir}}}}/s{i}", "fan_out": "none"}
        for i, name in enumerate(templates)
    ]
    doc = {"schema_version": 1, "pipeline": "p", "inputs": {"brief": "required"}, "stages": stages}
    manifest = root / "p.manifest.yaml"
    manifest.write_text(yaml.safe_dump(doc), encoding="utf-8")
    for name, text in templates.items():
        (root / name).write_text(text, encoding="utf-8")
    return tmp_path / "content", manifest


def test_token_is_replaced_with_the_content_root_posix_path(tmp_path):
    content, _ = _write(tmp_path, {
        "a.md": f"run {TOKEN}/scripts/nlm-preflight.py then {TOKEN}/x {{{{brief}}}}",
        "b.md": f"also {TOKEN}",
    })
    manifest = load_manifest(content, "p")
    for text in manifest.templates.values():
        assert TOKEN not in text
        assert content.as_posix() in text
    assert manifest.templates["a.md"] == f"run {content.as_posix()}/scripts/nlm-preflight.py then {content.as_posix()}/x {{{{brief}}}}"


def test_template_without_the_token_is_unchanged(tmp_path):
    content, _ = _write(tmp_path, {"a.md": "plain {{brief}}"})
    assert load_manifest(content, "p").templates["a.md"] == "plain {{brief}}"


def test_sha256_stays_over_the_manifest_bytes(tmp_path):
    content, manifest_path = _write(tmp_path, {"a.md": f"{TOKEN}/x"})
    assert load_manifest(content, "p").sha256 == hashlib.sha256(manifest_path.read_bytes()).hexdigest()
