# Notice

Claude-klabauter
Copyright 2025-2026 Dónal example-operator

This product includes software developed by Dónal example-operator.

## Licence

Licensed under the Apache License, Version 2.0, **with a "Commons Clause" License
Condition v1.0 attached**. The rider is part of the licence, not a separate document:

> Without limiting other conditions in the License, the grant of rights under the License
> will not include, and the License does not grant to you, the right to Sell the Software.

Any licence notice or attribution required by the Apache License must also reproduce that
Commons Clause notice. Because of the rider, this project is **source-available** — it is
not OSI-approved, and calling it otherwise misstates what you are permitted to do. The
`LICENSE` file at the root of this repository is authoritative over every summary,
including this one.

Internal use, modification, and redistribution of derivative works are unrestricted by the
rider. Selling the software itself — or a product or service whose value derives entirely
or substantially from it — is what the rider bars. See `COMMERCIAL.md`, which also
describes a **free written internal-use grant** available on request for organisations
whose policy blocks non-OSI licences.

## Third-party dependencies

Nothing in this list is vendored into this repository. Each is a declared runtime
dependency resolved by your package manager at install time, distributed under its own
licence by its own authors; consult each project for its terms.

| Package | Role in the engine |
|---|---|
| `pydantic` | Entity models and validation |
| `psutil` | Cross-platform process liveness (the Windows session-identity path) |
| `jsonschema` | In-process JSON Schema validation, no Node runtime required |
| `PyYAML` | Policy, threshold, and store configuration loading |
| `referencing` | Schema `$ref` registry construction |
| `typing_extensions` | Annotated types used by the entity models |

Test-time only: `pytest`, `pytest-xdist`.

## Vendored schema bundle

`coordinator_core/ops/emit/_vendor/cockpit-contract/` contains a vendored copy of the
Cockpit-contract JSON Schema bundle, pinned by SHA. That directory carries its own
`LICENSE` file stating the grant under which the bundle is redistributed here; read it
alongside this notice rather than assuming the root licence covers it by default.
