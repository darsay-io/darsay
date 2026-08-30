# Doctor golden provenance

`doctor_capabilities.json` freezes the public doctor contract emitted by the
repository implementation.

- Generator: `tests/support/doctor_cli_runner.py doctor capabilities --json`
- Contract version: 1.0.0
- Initial tool version: 0.13.0
- Canonicalization: `tool_version` and `identity.version` become
  `[TOOL_VERSION]`; JSON keys are sorted and rendered with two-space indentation.
- Network: disabled; generation needs no vault and creates no doctor evidence.

| Artifact | Deterministic | Platform-dependent | Volatility | Strategy |
|---|---:|---:|---:|---|
| Doctor capabilities JSON | Yes | No | 2/5 | Structured exact golden after version-field scrubbing |

The initial artifact was reviewed against the eight-check/three-fixer registry,
the exit table and write scope in `docs/DOCTOR.md`, and the report fields
asserted by `tests/unit/test_doctor.py`.

To update after an intentional contract change:

```bash
UPDATE_GOLDENS=1 .venv/bin/python -m pytest \
  tests/integration/test_doctor_safety.py::test_capabilities_match_golden
git diff -- tests/golden/doctor_capabilities.json
```

Review every diff before committing. Normal tests never update the golden.
