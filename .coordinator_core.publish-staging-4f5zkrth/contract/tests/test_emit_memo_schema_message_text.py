"""
test_emit_memo_schema_message_text — regression guard for the memo
JSON-Schema's `description` fields against the OSS publish scrub
(docs/plans/2026-08-12-message-text-stops-naming-an-unreachable-repo.md).

The scrub (setup/percolate-hooks/percolate-store.yaml) rewrites private-repo
codenames to non-navigable placeholders on publish. A rendered message that
directs a reader to go read/see a codenamed repo therefore breaks for an OSS
mirror reader who has none of our repos. This module's `description` fields
are rendered text — they ship inside cross-repo-memo.schema.json /
archived-memo.schema.json, consumed by agents and DoE-claude's routing hook
— so they are in scope for that discriminator.

Investigation for this chunk (C7c) found no navigation prose: the handful of
`DoE-claude` / `project-rag` mentions inside description strings were
historical attribution ("project-rag-em's inbox-blitz proposal", "DoE-
claude-local extension") or `repos.<key>` functional-identifier examples,
never a "go read <repo>" instruction. A follow-up EM ruling overrode that
disposition: this module's emitted JSON is a row in a later chunk's
rendered-message corpus, gated by register rule B7, which fires on ANY
REDACTION-class token in rendered text regardless of navigation intent — the
codenames were removed from every description accordingly (registry keys
`repos.doe_claude` / `repos.project_rag` are functional identifiers, stay).
This test locks that state in so a future edit re-introducing either
navigation prose OR a bare REDACTION-class codename mention is caught here
rather than only at OSS-publish time.

Negative-spec: this test does NOT assert against `setup/percolate-hooks/
percolate-store.yaml` (read-only for this plan) and does NOT touch the
shared rendered-message corpus module under `coordinator_core/bash_guards/
tests/` (a later chunk's row to add) — it is a narrow, module-local guard.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Iterator

from coordinator_core.contract.emit_memo_schema import emit_schemas

# Mirrors the plan's discriminator examples (DoE-claude, project-rag,
# opticon, delphi/delphipro, holodeck) — any codename followed closely by a
# navigation verb ("see"/"read"/"check"/"visit") is the broken shape a
# publish-scrub turns into a dead pointer.
_CODENAMES = ("DoE-claude", "project-rag", "opticon", "delphi", "holodeck")
_NAVIGATION_PATTERN = re.compile(
    r"\b(see|read|check|visit)\b[^.]{0,60}(" + "|".join(_CODENAMES) + r")",
    re.IGNORECASE,
)


def _iter_descriptions(node: Any) -> Iterator[str]:
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "description" and isinstance(value, str):
                yield value
            else:
                yield from _iter_descriptions(value)
    elif isinstance(node, list):
        for item in node:
            yield from _iter_descriptions(item)


class TestDescriptionsDoNotDirectReadersToUnreachableRepos:
    def test_no_navigation_prose_to_private_repos(self, tmp_path: Path) -> None:
        emitted = emit_schemas(out_dir=tmp_path)
        for name, schema in emitted.items():
            for description in _iter_descriptions(schema):
                match = _NAVIGATION_PATTERN.search(description)
                assert match is None, (
                    f"{name}.schema.json description directs an OSS reader "
                    f"to a private, scrub-rewritten repo "
                    f"({match.group(0)!r}): {description!r}"
                )

    def test_no_redaction_class_codenames_in_emitted_schema(
        self, tmp_path: Path
    ) -> None:
        """B7 (docs/plans/2026-08-12-message-text-stops-naming-an-unreachable-
        repo.md): a REDACTION-class codename mention in rendered text is
        broken regardless of navigation intent — attribution prose that
        scrubs to a non-navigable placeholder still names nothing an OSS
        reader can resolve. Registry keys (`repos.doe_claude`,
        `repos.project_rag`) are functional identifiers, not prose mentions,
        and are exempted below via `test_functional_repo_keys_still_present`
        rather than here."""
        redaction_tokens = ("DoE-claude", "project-rag", "opticon", "delphi", "holodeck")
        emitted = emit_schemas(out_dir=tmp_path)
        for name, schema in emitted.items():
            for description in _iter_descriptions(schema):
                for token in redaction_tokens:
                    assert token not in description, (
                        f"{name}.schema.json description carries "
                        f"REDACTION-class token {token!r} in rendered text: "
                        f"{description!r}"
                    )

    def test_functional_repo_keys_still_present(self, tmp_path: Path) -> None:
        """Guards against over-correction: `repos.doe_claude` /
        `repos.project_rag` registry-key examples inside `to_repo`'s
        description are functional identifiers, not navigation prose, and
        must stay."""
        emitted = emit_schemas(out_dir=tmp_path)
        to_repo_desc = emitted["cross-repo-memo"]["properties"]["to_repo"][
            "description"
        ]
        assert "repos.doe_claude" in to_repo_desc
        assert "repos.project_rag" in to_repo_desc


class TestSentByPresentAndOptional:
    """C7 (docs/plans/2026-08-13-session-identity-earns-its-keep.md):
    `sent_by` is a real, described property on the emitted memo schema, and
    is NEVER in the `required` array — additive-optional, mirroring
    `picked_up_by`'s own optional/never-required shape on the receive path.
    """

    def test_sent_by_property_present_with_description(self, tmp_path: Path) -> None:
        emitted = emit_schemas(out_dir=tmp_path)
        props = emitted["cross-repo-memo"]["properties"]
        assert "sent_by" in props
        assert props["sent_by"]["type"] == "string"
        assert props["sent_by"]["description"]

    def test_sent_by_never_required(self, tmp_path: Path) -> None:
        emitted = emit_schemas(out_dir=tmp_path)
        for name, schema in emitted.items():
            required = schema.get("required", [])
            assert "sent_by" not in required, (
                f"sent_by must never be a required property (schema={name!r}, "
                f"required={required!r}) — it is optional-additive, resolved "
                f"only at memo.send time and absent on every memo predating "
                f"this field."
            )
