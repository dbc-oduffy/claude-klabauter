# Plan Review Fix Prompt Template (v1)

> Stage `fix` of `plan-review.manifest.yaml`, one domain reviewer per plan. Placeholders are the
> closed set only; every other input is a field of the brief file at `{{brief}}`.

## Template

```
You are the domain reviewer for one plan: {{subject}}.

## Your Inputs

**Brief:** {{brief}}
**Plan under review:** {{subject}}
**Claims check:** {{scratch_dir}}/{{subject}}-claims.md

## Your Job

1. Read the plan and its claims check.
2. Fix every wrong claim in the plan, and every domain defect you find, in place.
3. Write {{scratch_dir}}/{{subject}}-fix.md: what you changed and what you left, each with a reason.
```
