# Doctor process-safety fixtures

These fixtures make the high-risk `darsay doctor` guarantees reproducible from
the repository alone. The contract source is `docs/DOCTOR.md`, doctor contract
version 1.0.0. Run the complete layer with:

```bash
.venv/bin/python -m pytest tests/integration/test_doctor_safety.py
```

## Coverage matrix

| Contract requirement | Level | Test | Status |
|---|---|---|---|
| Prepared intent survives SIGKILL before target commit | MUST | `test_sigkill_recovery_is_explicit_and_byte_exact[after-prepare]` | Covered |
| A committed target with no terminal event is recoverable | MUST | `test_sigkill_recovery_is_explicit_and_byte_exact[after-commit]` | Covered |
| Two concurrent fixers produce one winner and one exit-5 loser | MUST | `test_two_process_fix_has_one_winner_and_one_concurrency_loser` | Covered |
| Repeating a successful fix takes zero additional actions | MUST | `test_fixture_fix_is_idempotent_across_processes` | Covered |
| Undo restores pre-fix bytes and metadata | MUST | `test_fixture_fix_and_undo_restore_corrupt_bytes` | Covered |
| Private diagnosis evidence does not change detector findings | MUST | `test_detector_result_is_unchanged_by_its_private_evidence` | Covered |
| The public capabilities contract matches its reviewed golden | MUST | `test_capabilities_match_golden` | Covered |

This is 7/7 coverage for the repository-owned process-safety contract. It is
not a claim that every doctor detector has a retained process fixture. Ordinary
unit and integration tests cover all three current fixer families; this full
process fixture currently targets generated README repair, the deterministic
write fixer with the richest crash boundary.

`corrupt.sh` builds a real bundle through the hermetic `test:` provider, changes
only its generated README, and snapshots the corrupted state. `assert.sh`
checks the repaired projection against the real renderer. Neither script uses
the network.
