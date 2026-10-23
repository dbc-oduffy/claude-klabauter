---
deliverable_id: dlv-brief-trim-fixture-abc123
prime_exit_criterion:
  statement: Briefs carry no raw whole-file dumps.
---
# Brief trim fixture plan

## Problem

Three rows write 2, 5 and 8 python files so the executor briefs carry symbol spans.

## Tasks

```yaml plan-tasks
- id: C1
  title: two-file row
  change_kind: code-edit
  surface: pkg/a1.py
  body: |
    Change `alpha_1` in pkg/a1.py and keep pkg/a2.py consistent.
  writes:
    - pkg/a1.py
    - pkg/a2.py
- id: C2
  title: five-file row
  change_kind: code-edit
  surface: pkg/b1.py
  body: |
    Change `beta_1` in pkg/b1.py; the other four files follow it.
  writes:
    - pkg/b1.py
    - pkg/b2.py
    - pkg/b3.py
    - pkg/b4.py
    - pkg/b5.py
- id: C3
  title: eight-file row
  change_kind: code-edit
  surface: pkg/c1.py
  body: |
    Change `gamma_1` in pkg/c1.py; the other seven files follow it.
  writes:
    - pkg/c1.py
    - pkg/c2.py
    - pkg/c3.py
    - pkg/c4.py
    - pkg/c5.py
    - pkg/c6.py
    - pkg/c7.py
    - pkg/c8.py
```
