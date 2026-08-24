# HANDOFF — implement incremental archiving

You are implementing the design in **`docs/INCREMENTAL.md`**. Read that
file first — it is the spec and this handoff does not repeat it. Read
`CLAUDE.md` for the invariants; they all still apply, especially
verify-before-register, record-don't-fabricate, and export determinism.
Delete this handoff file in your final commit (pattern: HANDOFF-datasets.md).

**Versions:** `__version__` 0.5.0, `SCHEMA_VERSION` 1.4.0 (additive — add a
changelog comment in `__init__.py`). `MVB_FORMAT_VERSION` unchanged.

**Out of scope:** `archive --include` subsets (QUANTIZATION.md), multi-source
fetch, `hydrate --quantize`.

## Suggested shape

- New module `src/modelvault/transfer.py`: ledger load/save (atomic — write
  temp file, `os.replace`, same directory), `transfer.lock` (pid/host/started;
  stale if pid dead on same host), reconcile, plan/report, the transfer loop,
  session records. `archiver.archive_model` stays the orchestrator: the pin
  phase reuses the existing `model_info`/`dataset_info` call (including the
  1.3.0 gated/not-found handling and `parse_base_model_tags`), the register
  phase reuses the existing manifest assembly but takes `file_records` from
  the ledger instead of a final hashing pass.
- At pin time, store in the ledger everything registration needs that came
  from the API: expected file list (path/size/`lfs_sha256`/`git_sha1`),
  card_data dict, tags, `gated`, `created_at`/`last_modified`. Registration
  then reuses the pinned copies (they describe the pinned revision — more
  correct than re-reading a card that may have moved) and makes live calls
  only for the ecosystem queries.
- Replace the single `snapshot_download` with per-file
  `hf_hub_download(repo_id, filename, revision=commit, local_dir=payload_dir,
  repo_type=...)`. The library's `local_dir` mode keeps partials and resume
  metadata under `<payload>/.cache/huggingface/` — that is the byte-level
  resume; do not build your own Range client.
- CLI (`archive` flags): `--dry-run`, `--max-gb N` / `--max-bytes SIZE`
  (suffix parsing: `500M`, `20G`), `--max-minutes N`, `--rehash`, `--jobs N`
  (default 4). SIGINT handler: finish the in-flight ledger write, record the
  session with `end_reason: "interrupt"`, exit 10. Exit codes per spec:
  0 complete/registered, 10 clean partial, 1 error.
- Small-file pool (< 8 MiB) via `ThreadPoolExecutor`; ledger writes happen
  only in the main thread (workers return results, main thread records).
  Large files sequential, ascending size, path tie-break.
- `cmd_list`: bundles with `transfer.json` but no `manifest.json` render an
  in-progress row (`archiving: 61% (34.1/55.6 GB, 9/15 files verified)`).

## Gotchas — read before coding

1. **`.cache/huggingface/` now lives until registration.** Today
   `archive_model` deletes it right after download; move that deletion into
   the register phase. Everything that walks the payload must skip `.cache/`
   until then: `hashing.iter_payload_files`, completeness checks, metadata
   extraction globs (`_measured_row_counts` uses `rglob("*.parquet")` —
   guard it), and reconciliation itself.
2. **Adjust the 1.3.0 gated cleanup.** Current behavior removes the partial
   payload on `GatedRepoError` during download. Under this design partials
   are valuable: keep that cleanup ONLY for pin-time failure (no ledger
   exists yet). A mid-transfer gate flip (ledger exists) keeps everything,
   logs an `events` entry, ends the session as `error` with the gated
   message plus "the partial archive resumes if access returns".
3. **Export/verify exclusions.** `transfer.json` and `transfer.lock` must
   join `exports.json`/`hydration.json` in `export.py`'s exclusion list, and
   `verify` must ignore them. Check `.mvb.tar` determinism after (export
   twice, compare SHA-256).
4. **Existing-bundle check stays manifest-based.** Ledger-without-manifest is
   the resume path, not an error. `--force` on a registered bundle = fresh
   pin whose reconcile adopts verified bytes (cheap re-verify + manifest
   rebuild); it remains the one sanctioned write into a registered payload.
5. **Digest source of truth:** compare against `lfs_sha256` when present,
   else `git_sha1` (compute via `hashing.hash_file(with_git_sha1=True)`;
   blake3 stays optional and recorded when available).
6. **`utc_now()` everywhere; no naive datetimes.** Session/ledger timestamps
   follow the manifest's ISO-8601-with-offset convention.
7. **Machine guardrail:** the local dcg hook blocks `rm -rf` and
   inline-python compound shell commands. Use Python (`shutil`) inside the
   tool for deletions, the Write tool for scripts, and run them as
   `.venv/bin/python <script>`.
8. `--vault` is a global flag: `modelvault --vault DIR archive REPO`.

## Suggested order of work

1. `transfer.py` core (ledger, reconcile, per-file loop) wired into
   `archive_model`; an unbudgeted run must behave like today, end to end.
2. Budgets, SIGINT, exit codes, `--dry-run` plan report (reuse `estimate`'s
   disk-headroom check).
3. `--rehash`, `--jobs`.
4. `list` in-progress rows; export/verify exclusions.
5. Local sources: sibling-manifest LFS index, clonefile-with-fallback copy
   (macOS: try `cp -c` subprocess or ctypes clonefile; plain copy
   otherwise), `mirrors_used` + ledger attribution.
6. Docs, versions, validation sweep, delete this file.

## Validation checklist (no test suite — CLI against scratch vaults)

Use the session scratchpad for all scratch vaults. Reference repos:
`sshleifer/tiny-gpt2` (KB-scale, multi-file), `Qwen/Qwen3-0.6B` (~1.4 GB,
for a real mid-file interrupt), dataset
`datasets/cornell-movie-review-data/rotten_tomatoes`.

1. Unbudgeted fresh archive of tiny-gpt2 → registered, schema 1.4.0,
   `source.transfer.sessions == 1`; payload `bundle_hash` and per-file
   hashes identical to a pre-change archive of the same revision.
2. `--max-bytes 200K` on tiny-gpt2 → exit 10, in-progress `list` row;
   re-run repeatedly → exit 0; session records sum correctly.
3. SIGINT mid-shard on Qwen3-0.6B → partial under `.cache`; resume session's
   `bytes_network` is visibly less than the shard size (Range resume proven).
4. Delete `transfer.json` mid-archive → next run reconciles: previously
   verified files adopted with zero network, archive completes.
5. `--dry-run` at every stage: correct counts, no payload bytes moved.
6. Truncate one verified payload file, run `--rehash` → demoted, re-fetched,
   `events` entry recorded.
7. Pin-time gated failure still clean (HF_HOME pointed at an empty scratch
   dir = anonymous; use a known gated repo — nothing downloads).
8. Registered bundle: `export` twice → byte-identical; `verify`, `regen`,
   `smoke` all pass; `info` unaffected.
9. Local sources: pick two commits of tiny-gpt2 (list refs via the API) that
   share weight blobs; archive both; second shows `source: "local:<id>"` in
   its ledger and a populated `mirrors_used`.
10. Dataset flow: budgeted archive of rotten_tomatoes to completion.

## On completion

- `docs/MANIFEST.md`: `source.transfer`, `mirrors_used` note,
  `checksum_verification.method`, header/version bumps.
- `docs/INCREMENTAL.md`: flip the status line to implemented; fix any drift
  between spec and what you built (the doc must describe reality).
- `CLAUDE.md`: docs-list line for INCREMENTAL.md → implemented wording.
- Auto-memory: update `model-vault-project.md` (incremental archiving
  implemented, version/schema, anything a future session must know).
- Commit per the repo's style (imperative summary line; single-purpose
  commits; Claude co-author trailer), delete this handoff in the final one.
