<!-- 1-2 sentence summary. Why, not what. -->

## What

<!-- The change in one bullet list. -->

## Why

<!-- The user-facing reason. Link to issue if there is one. -->

## Verification

<!-- For data/pipeline changes: paste the new manifest.json (or a diff). -->
<!-- For code changes: `pytest -q` output is enough. -->

<details>
<summary>manifest.json (or test output)</summary>

```
paste here
```

</details>

## Checklist

- [ ] Tests added or updated
- [ ] Architecture invariants honoured (see `docs/architecture.md`)
- [ ] If touching `sources/coverage.py` or `ops/intersect.py`, the honesty
      invariant (`covered=false ≠ depth=0`) is still tested
