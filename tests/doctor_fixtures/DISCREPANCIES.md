# Doctor process-harness discrepancies

## DISC-001: Process-fixture breadth

- **Reference:** `docs/DOCTOR.md`, doctor contract 1.0.0.
- **Current implementation:** the complete subprocess/SIGKILL harness uses the
  generated-README fixture. Transfer-lock and hydration quarantine have
  repository unit/integration coverage but not their own complete subprocess
  fixture directories.
- **Impact:** no known contract divergence; process-level coverage is narrower
  than detector/fixer coverage.
- **Resolution:** INVESTIGATING — add fixtures when they contribute a distinct
  crash or coordination boundary instead of cloning the same assertions.
- **Tests affected:** none are skipped or expected to fail.
- **Review date:** 2026-08-30.
