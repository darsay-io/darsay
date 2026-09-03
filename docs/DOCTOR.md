# Doctor

`darsay doctor` is the offline health and repair surface for an entire vault.
It is separate from `darsay verify <bundle>`: `verify` re-hashes one immutable
payload and records that result in the bundle, while `doctor` examines
operator configuration and vault metadata without changing payload bytes.

```bash
darsay doctor                         # diagnose; exit 1 when findings exist
darsay doctor --json                  # stable machine-readable report
darsay doctor --fix                   # low-risk, journaled repairs
darsay doctor --fix --dry-run         # proposed actions only
darsay doctor undo latest             # reverse the latest mutation run
darsay doctor capabilities --json     # checks, fixers, write scope, exits
darsay doctor health --json           # shallow <200 ms target; no artifacts
```

The doctor never uses the network. It never changes files under `model/` or
`data/`, never writes `curation.md`, never guesses missing archival facts, and
never escalates privileges. A normal diagnosis writes only private evidence
under `<vault>/.doctor/`.

## Checks and repairs

The default pass checks:

- whether the vault is readable and configuration TOML parses;
- whether registered manifests are readable and supported;
- whether immutable payload paths, sizes, and (unless `--quick`) SHA-256
  digests match each manifest, without writing verification history;
- whether generated `README.md` files match `manifest.json` plus
  `curation.md`;
- whether each bundle's `SHA256SUMS` is its inventory in coreutils format
  (a stat-free check that runs under `--quick` too);
- whether a transfer lock is malformed, copied, or owned by a dead local
  process;
- whether disposable `hydration.json` records parse and name an existing
  interpreter.

Only four low-risk repair families are enabled:

1. Regenerate a derived bundle `README.md` from its authoritative inputs.
2. Regenerate a derived `SHA256SUMS` from the manifest's inventory.
3. Quarantine a stale transfer lock after proving it is malformed, copied, or
   owned by a dead process on this host.
4. Quarantine an unreadable/stale hydration record. Hydration is disposable;
   the next `run` or `hydrate` rebuilds it.

Malformed manifests and configuration remain manual findings. The doctor
will not reconstruct them from guesses. A live lock and a lock owned by a
different host are also left alone because local process inspection cannot
prove that remote owner is dead.

### Current diagnostic scope

The nine checks above are the complete current registry, not a claim to cover
every possible damaged archive or runtime state. In particular, doctor does not
yet provide dedicated findings for transfer-ledger semantics, interrupted
imports, MVB structure, shared runtime-environment leases, installed package or
completion drift, or foreign-host lock staleness that cannot be proven locally.
Those states remain manual/refusal territory. `explain --json` describes check
policy but does not currently publish fixture or input schemas for constructing
damaged states.

## Mutation and undo

Every diagnosis or mutation obtains one non-blocking vault-wide doctor lock.
Fix and undo also preflight every affected bundle and hold the project's
per-bundle `transfer.lock` before the first target mutation. A live project
writer therefore blocks the whole repair transaction with exit 5, rather than
allowing an earlier bundle to be partially fixed. A stale transfer lock must be
quarantined in a separate pass before other fixes in that bundle. Before a
write or quarantine, the doctor:

1. resolves the target inside the vault and refuses symlink/traversal escapes;
2. copies existing bytes to the run's private `backups/` tree and verifies the
   copy;
3. appends and fsyncs the intended action in `actions.jsonl`;
4. uses an atomic same-directory replace or rename;
5. verifies the resulting hash where a file was written;
6. appends and fsyncs a terminal success or failure event.

The mutation layer accepts only the exact filename/operation pairs declared by
the built-in fixer registry: write `README.md`, rename `transfer.lock`, or
rename `hydration.json`. Undo accepts only its restore/quarantine operations on
those same three canonical bundle paths. A journal cannot authorize a write to
`manifest.json`, payload, or an arbitrary in-vault file.

`doctor undo RUN_ID` reads actions in reverse. It restores only when current
state still matches either the recorded pre-fix or post-fix hash; unrelated
later edits cause exit 4. Files created by a repair are quarantined during
undo instead of being deleted. Undo itself is another journaled run, so it can
be inspected or reversed.

If a process is terminated after the prepared journal record but before its
terminal record, subsequent diagnosis, fix, undo of another run, and evidence
garbage collection fail closed with exit 4. The error names the interrupted
run and prints an explicit `doctor undo RUN_ID --strict` recovery command.
That undo compares the durable before/after hashes, restores from the verified
backup when safe, and records the entire recovered action set with one durable
batch marker. If that marker cannot be written, it compensates the inverse
mutations before reporting failure. Recovery may reclaim only a same-host,
same-bundle lock explicitly marked as owned by a dead darsay doctor process;
ordinary stale, live, and foreign locks remain untouched.

The repository process-safety suite kills real doctor processes immediately
after durable intent and immediately after target commit. It also exercises
two-process contention, idempotence, byte-exact undo, detector repeatability,
and a reviewed golden of `capabilities --json`; see `docs/TESTING.md`.

## Evidence

Each diagnose, fix, or undo run creates:

```text
<vault>/.doctor/
├── latest -> runs/<run-id>
├── doctor.lock
├── scorecard_history.jsonl
└── runs/<UTC>__<hash6>/
    ├── run.json
    ├── report.json
    ├── report.md
    ├── scorecard.json
    ├── actions.jsonl
    ├── backups/
    ├── quarantine/
    ├── stdout.json
    ├── stderr.log
    └── undo.sh
```

Doctor directories are mode `0700`; verbatim backups are mode `0600`.
Reports redact common secret assignments and never dump the environment.
List or compare history with `doctor ls` and `doctor diff`. Evidence retention
is explicit: `doctor gc --before 2026-01-01 --yes` retains the latest run and
removes only older doctor run directories.

## Automation contract

Use `doctor capabilities --json` for discovery and `doctor robot-docs` for the
short automation policy. JSON keys, check IDs, fixer IDs, and exit meanings are
stable within schema version 1. Reports state `artifacts_created`,
`network_attempts`, and `target_actions` explicitly so automation does not need
to infer side effects from the command name. `health --json` always reports
`false`, `0`, and `0` for those fields. Human output is not a parsing interface.

| Exit | Meaning |
|---:|---|
| 0 | Healthy, or the requested fix/undo completed |
| 1 | Findings detected by a diagnose-only run |
| 2 | Diagnosis was partial, or manual findings remain after fix |
| 3 | A fix failed and rollback was attempted |
| 4 | Unsafe mutation or undo refused |
| 5 | Another doctor mutation holds the lock |
| 6 | Requested online diagnostics are unavailable |
| 64 | Usage error |
| 66 | Input or run not found |
| 73 | Artifact or lock cannot be created |
| 74 | I/O failure |

Useful bounded controls include `--only`, `--skip`, `--severity`, `--budget`,
and `--quick`. `--since ISO_TIMESTAMP` scans only bundles with newer file
metadata (configuration is still checked unless excluded). `--only` and
`--skip` bound detector execution, not merely displayed output.
`--robot-triage` emits one JSON diagnosis and proposed repair plan; it never
fixes. A missing vault is reported without creating the misspelled path.
`undo` and `gc` apply the same no-create rule before opening a doctor lock.
