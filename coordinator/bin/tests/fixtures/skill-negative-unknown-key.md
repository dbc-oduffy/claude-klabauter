---
name: test-skill-fixture
description: Synthesises handoff state and session work into a structured daily summary.
unknown_extra_field: should-be-rejected
---
Negative fixture — unknown extra key present. Verifies that the skill schema's `additionalProperties:false` constraint rejects records carrying keys outside the defined property set. The `unknown_extra_field` key above is intentionally not in the skill schema.
