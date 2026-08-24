# Handoff: implement dataset bundles

You are in `~/personal/model-vault`. Implement the dataset-bundle design in
`docs/DATASETS.md` — it is the spec and the authority; this handoff adds
implementation order, audit results, and validation gates. Where they
disagree, the spec wins; where the spec is silent, CLAUDE.md's invariants
decide.

## Read first (in this order)

1. `docs/DATASETS.md` — the spec (design commit `4ed8095`).
2. `CLAUDE.md` — invariants; do not break any of them.
3. `src/modelvault/schema.py`, `archiver.py`, `verify.py`, `export.py`,
   `estimate.py`, `licensing.py`, `smoke.py`, `readme_gen.py` — the touch
   surface. `docs/MANIFEST.md` for the schema you'll extend.

## Hard gates (check after every step)

- **G1 — model bundles are byte-identical.** The reference bundle
  `vault/qwen--qwen3-0.6b/c1899de289a0` must pass
  `.venv/bin/modelvault verify` before and after your changes, and a fresh
  `archive sshleifer/tiny-gpt2` into a scratch `--vault` must produce a
  manifest identical in shape to today's (only timestamps/host may differ).
- **G2 — export determinism.** Export any bundle twice; the two `.mvb.tar`
  SHA-256s must match. Re-check after the payload-root work and after the
  dataset work.
- **G3 — record, don't fabricate.** Unknown = `null`. Upstream claims
  (split sizes from `dataset_infos.json`, card YAML) are recorded as
  *declared*; only facts established from the payload are *measured*. All
  query caps recorded.

## Implementation order

### Step 1 — payload-root parameterization (the only step touching existing behavior)

Add `"payload_root": "model/"` to the `model` entry in `ARTIFACT_TYPES`.
Add a helper (in `schema.py`) like `payload_root(manifest)` that reads
`manifest["inventory"]["layout"]["payload_root"]`, falling back to
`"model/"`, returning the name without the trailing slash where a dir name
is needed. Writers take the root from the registry; readers from the
manifest; no literal `"model"` remains at these audited sites:

- `archiver.py:96` (payload dir), `:129` (record path prefix), `:141`
  (inventory paths already derived), `:205` (layout.payload_root — write
  from registry)
- `verify.py:29,35` — `export.py:166–167` (import-side re-hash) —
  `smoke.py:79` — `licensing.py:135` — `estimate.py:127` (prefix) —
  `hydrate.py:316` (payload dir)
- `hashing.py:72` — generalize the `"covers"` string to name the payload
  root generically.

Leave `hydrate.py`'s `ENGINES` detect/weights globs (`model/*`) exactly as
they are: they intentionally never match a `data/` inventory, which is the
designed graceful degradation. Run gates G1+G2 before proceeding.

### Step 2 — Hub-address refs

`parse_repo_ref(s) -> (repo_type, repo_id)` accepting: `owner/name` →
model; `datasets/owner/name` → dataset; `https://huggingface.co/owner/name`
and `https://huggingface.co/datasets/owner/name` (any trailing path/query
stripped) → same. Anything else: clean `SystemExit`. Wire into `estimate`
and `archive` only (bundle-path commands dispatch on the manifest and need
nothing). Plumb `repo_type`: `HfApi.dataset_info(..., files_metadata=True)`,
`snapshot_download(..., repo_type="dataset")`,
`bundle_dir_for` gains the `datasets--` prefix for datasets (spec §5).

### Step 3 — the dataset artifact type

- `ARTIFACT_TYPES["dataset"]`: `payload_root: "data/"`; required rule
  `data` matching `data/*.parquet`, `*.jsonl`, `*.json`, `*.csv`,
  `*.arrow`, `*.txt`, `*.tsv` (fnmatch `*` crosses `/`, so these match
  nested files); recommended: card (`data/README.md`), license globs,
  `data/dataset_infos.json`.
- `dataset_metadata` extractor (new module or `metadata.py` sibling
  function; offline, payload-only): formats table from the inventory;
  configs/splits/features/example counts parsed from
  `data/dataset_infos.json` and card YAML → recorded under `declared`;
  `measured` row counts only if pyarrow imports (optional; else
  `{"status": "skipped", "reason": ...}` — never crash).
- Relationships: dataset side `models_trained_on` via
  `api.list_models(filter=f"dataset:{repo_id}", limit=100)` with
  `query_limit` recorded; model side gains `training_datasets` from
  `card.get("datasets")` (normalize str→list).
- `archiver.archive_model` grows a `repo_type` parameter (or a shared
  internal path) — keep ONE archive flow with per-type dispatch via the
  registry, not a parallel copy.
- `readme_gen`: dataset branch for the derived README (formats/splits
  instead of params/runtime); curation template unchanged.
- `estimate`: dataset branch prints a formats breakdown where models show
  parameters; skips engines row or prints per-type equivalent; archive
  hint prints the `datasets/...` ref.
- Bump `SCHEMA_VERSION` to `1.2.0` and `__version__` to `0.4.0`. Update
  `docs/MANIFEST.md` (new sections/fields, per-type presence rules),
  top-level `README.md` (usage lines + a short Datasets paragraph), flip
  `docs/DATASETS.md` status to implemented, keep `CLAUDE.md` layout
  accurate. MVB format: no version bump; add a doc note that
  `artifact_type` rides in the embedded manifest.

### Step 4 — dataset smoke + end-to-end validation

Stdlib-only structural checks in `smoke.py` via per-type dispatch: parquet
`PAR1` magic at head and tail, first-line JSONL `json.loads`, CSV header
sniff. Then validate end-to-end **on a small dataset first** (pick a public
parquet dataset under ~20 MB — verify its size with
`estimate datasets/<id>` before archiving):

    estimate datasets/<small> ; archive into a scratch --vault ; verify ;
    export twice (hashes match) ; import ; smoke ; list/info/regen

`estimate datasets/saidutta69/fable-5-premium` (metadata only) must also
work. **Do not download fable-5-premium (2.3 GB) or any large repo without
Jeremy's go-ahead** — the spec plans it as the reference dataset bundle,
but the spend is his call.

## Machine notes

- Use `.venv/bin/modelvault` / `.venv/bin/python` (Python 3.14; no `uv`).
- A `dcg` guard blocks `rm -rf`, inline `python3 -c` in compound commands,
  and `$(cat <<EOF)` substitutions: Write scripts/commit-messages to files
  (`git commit -F <file>`); clean scratch vaults by asking Jeremy or using
  paths under the session scratchpad.
- No test suite: validation is the CLI runs above plus the gates.
- Commit only when Jeremy asks; style: imperative summary + short body,
  trailer `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>`.

## Done when

- [ ] G1/G2/G3 hold; reference model bundle verifies clean.
- [ ] Small dataset round-trips: archive → verify → export×2 → import → smoke.
- [ ] `estimate` works on both ref forms and full URLs, models and datasets.
- [ ] Model manifests gain `relationships.training_datasets`; dataset
      manifests carry `dataset_metadata` + `models_trained_on`.
- [ ] Schema 1.2.0 / v0.4.0; MANIFEST.md, README.md, DATASETS.md, CLAUDE.md
      all updated; docstrings in `cli.py` list nothing stale.
- [ ] This file deleted (its job is done).
