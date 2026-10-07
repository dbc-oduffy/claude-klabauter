"""The kind-enum refusal points reply-writers at in_reply_to."""

from coordinator_core.ops.fleet._outbox_frontmatter_rules import validate_outbox_frontmatter


def test_invalid_kind_error_names_in_reply_to():
    errors = validate_outbox_frontmatter({"kind": "reply"})
    assert any("in_reply_to" in e for e in errors), errors
