# Classification — preservation evidence and archive selection

The classifier decides which published weight sets to retain when a fresh
model archive is pinned. A **negative** is a preservation candidate retained
by a conservative rule; a **print** records an established duplicate or
derivative relationship; **unknown** means the evidence does not establish
either verdict. These labels do not prove originality or recoverability.
Support files are always retained.

A verdict and a fetch decision are separate facts. The default retains
negatives, unknowns, support, and prints without a retained same-bundle
weight twin. It skips only R15 duplicates. Classification never
deletes archived bytes, chooses a preferred precision, or reduces a
GGUF pack to one canonical model.

See [Quantization](../QUANTIZATION.md) for the collector's view,
[Manifest](../MANIFEST.md) for the durable selection record, and
[Catalogs](../CATALOGS.md) for size and verdict summaries.

## Surfaces and lifecycle

```bash
darsay classify owner/model                  # evidence, verdicts, actions
darsay classify owner/model --json           # structured audit
darsay estimate owner/model                  # price the archive selection
darsay archive owner/model                   # pin and fetch that selection
darsay archive owner/model --full            # every published file
darsay archive owner/model --include '*Q4_K_M*' # explicit selection
```

The default applies to models. Explicit includes and `--full` bypass
classification; datasets retain their normal acquisition behavior.
A fresh pin records its selection, and subsequent transfers resume the
same files. `--force` deliberately creates a new pin under current
rules. A classification failure or an unverifiable selection falls back
to the full repository, with the reason reported.

`classify` and single-source `estimate` read metadata and bounded
headers without writing a bundle. Unknown verdicts are audit findings,
not command failures. The report names each set, its files and known
bytes, the rule, evidence, and whether it will be fetched or skipped.

## Sets and shard boundaries

Classification is over sets of files:

- Each directory's readable weight index selects an indexed set.
  Safetensors outside a readable safetensors index in that directory
  form an orphan set. Without such an index they form a standalone set;
  an unreadable index marks the unclaimed set as uncertain.
- A GGUF variant is one file or the entire numbered shard group sharing
  a directory, name, and declared shard count. Every declared shard must
  occur exactly once. An incomplete group is unknown and retained.
- Other `.bin`, `.pt`, and `.pth` weights form indexed or per-directory
  sets. All remaining files form the support set.

A split GGUF is classified as a whole. Every shard's header
must be readable within the caps before a content-based verdict is possible.
Where present, `split.count` and `split.no` must agree with the numbered
files. An importance matrix found in any shard applies to the group;
an external source claim or an unreadable shard leaves the source evidence
unresolved. No GGUF conversion is automatically assumed reproducible.

The separate GGUF variant inventory groups model weights for comparison
and selection. It excludes `mmproj` projectors: these are companions,
not another copy of the model. Variant grouping and precision labels
establish no preservation verdict by themselves.

## Rules

Every verdict records its rule identifier and reason. The ordering
described after the table determines which rule applies when several
conditions are present.

| Rule | Evidence | Verdict and default action |
|---|---|---|
| R1 | Non-weight files | `support`; fetch. |
| R2 | Every file in a safetensors or other non-GGUF weight set has an upstream LFS SHA-256 matching a file in the declared base | `print`; fetch. Remote identity does not prove retained local bytes. |
| R3 | Index-selected safetensors with F64/F32/F16/BF16 dtype and no quantization config | `negative`; fetch. |
| R4 | Standalone safetensors with F64/F32/F16/BF16 dtype | `negative`; fetch. |
| R5 | Safetensors with a quantization config or a dominant dtype outside F64/F32/F16/BF16 | `negative`; fetch because calibration or a curated recipe may be baked in. |
| R6 | F64/F32/F16/BF16 safetensors excluded from the readable index in their directory | `unknown`; fetch because these may be a distinct build. |
| R7 | A complete, readable GGUF group contains any `quantize.imatrix.*` key | `negative`; fetch. |
| R8 | A readable GGUF group without an importance matrix records an external source URL or Hugging Face repository | `unknown`; fetch. |
| R9 | A readable GGUF group without an importance matrix or external source has exactly one candidate source set and no uncertain source sets | `unknown`; fetch. Co-location does not establish complete inputs, a recipe, or verified reconstruction. |
| R10 | The same GGUF prerequisites, with no candidate non-GGUF conversion source | `negative`; retain this published GGUF variant. |
| R11 | The same GGUF prerequisites, with several candidate source sets or any uncertain source set | `unknown`; fetch because the source build is ambiguous. |
| R12 | Other non-GGUF weight formats alongside safetensors | `unknown`; fetch because equivalence is not established. |
| R13 | Other non-GGUF weight formats without safetensors | `negative`; fetch. |
| R14 | Required config, index, dtype, or header evidence is unavailable; a GGUF group is incomplete or has inconsistent split metadata | `unknown`; fetch and record the reason. |
| R15 | Non-GGUF weight sets have identical complete multisets of upstream LFS SHA-256 hashes, and one can be retained | Retain one deterministic copy; the other sets are `print` and skipped, recoverable from the retained twin. |

Safetensors and other non-GGUF sets are judged first. R2 precedes
dtype and index checks; unreadable required metadata then yields R14.
R5 precedes the floating-point indexed/standalone/orphan distinction.
R15 deduplicates identical sets before candidate counting.

Candidate source sets carry R2, R3, R4, R6, R12, or R13. R14 sets are
counted separately as uncertain; calibrated quantized sets under R5
are not conversion sources. GGUF checks proceed from complete, readable
headers through R7, R8, uncertainty, then the candidate count.
R10 recognizes supported safetensors and other non-GGUF candidate source
sets. A BF16 GGUF in the same pack does not itself establish a conversion
source for every other variant. R10 preserves each published GGUF
variant; it does not prove original-release status or the absence of a
higher-fidelity copy in this repository or elsewhere.

R2 establishes identity against the declared base's upstream inventory,
not against a verified retained dependency. A bundle registered under
that base's address may hold a different revision or selection. The
classifier does not use vault presence as permission to omit weights.

R7 records importance-matrix metadata, not proof that the calibration
corpus is private or reconstruction impossible. R3 and R4 record dtype
evidence, not proof of training precision or publisher originality.

## What a skipped print promises

Only R15 skips automatically. Every skipped weight file's content has a
hash-identical twin in a retained weight set in this same bundle. The
comparison covers every file present in each set; it does not establish
that upstream supplied every shard needed for a complete model. The
omitted upstream paths remain documented in the full inventory, but are
not materialized; secondary pipeline layouts may depend on them. Use
`--full` to keep every published file and layout.

R9 makes no reconstruction promise. Even a complete colocated source
would not establish the toolchain, conversion settings, calibration
inputs, or output hashes. Functional recreation and byte-exact recovery
are different outcomes. Future recipe-based omission requires tested
recovery evidence and portable retained dependencies, not just a declared
parent or a recipe name.

## Bounded evidence gathering

`collect_facts` obtains upstream inventory, configs, indexes, weight
headers, and any declared base inventory through the provider.
`build_sets`, `evaluate`, and `attach_selection` operate on these facts
without network access.

`SourceProvider.read_bytes(source, revision, relative, start, length)`
provides bounded range reads. A denied, malformed, truncated, unsupported,
or capped read is recorded as unavailable evidence. It cannot justify a
skip. Authentication uses the provider's normal credentials.

Current per-source defaults are:

| Limit | Value |
|---|---|
| Config or index JSON | 64 MiB per file |
| GGUF header bytes fetched | 64 MiB per file |
| GGUF and safetensors header files combined | 64 |
| Concurrent header readers | 8 |
| Safetensors header sanity bound | 100,000,000 bytes |

GGUF headers are processed first in path order; remaining header-file
capacity serves safetensors. Safetensors reads target unclaimed files
and one member of a set when its config does not establish dtype.
The GGUF parser traverses the complete key/value table, skipping bulk
numeric arrays by offset and reading string arrays as needed. A small
first shard is not evidence about the later shards.

The read receipt records request count, fetched bytes, header files read,
whether the base inventory was obtained, and the configured JSON, GGUF,
and header-file caps. These are per-source limits, not a promised total
budget for a whole catalog refresh.

## Verified selections and records

The classifier first tries compact globs, then root-anchored globs,
then literal-escaped explicit paths, including every retained support
file. Each candidate is evaluated with actual subset acquisition semantics
against the complete upstream inventory. The entire selected path set
must equal the intended retained paths exactly, not just the weight paths.
Automatic classification preserves arbitrary support files; the narrower
sidecar list applies to the curator's explicit `--include` selection.

If no selection can be verified, or all weight sets would be omitted,
the archive retains the full repository and reports why. An uncertain
shard cannot disappear through a convenient glob.

A classified subset records `source.subset.policy: "negatives"`, the
classifier version, read receipt, per-set verdicts and actions, and
the full upstream inventory with sizes and hashes. That policy name
does not claim every retained byte is a negative. Existing payloads
remain unchanged; the operation only controls acquisition.

## Sizes and catalog summaries

Catalog schema **3.0.0** stores `size_basis` separately from
`classification`:

- `repository` prices the complete upstream inventory.
- `selection` prices the explicit or pinned file selection.
- `archive` prices the classified retention decision.

`payload_bytes` is the known-byte sum at that basis. A nonzero
`unknown_size_count` makes it a lower bound. `repository_bytes` is the
exact whole-repository total, or null when any file size is unknown.

The classification digest contains per-verdict set/file/byte counts,
`skipped_bytes`, and `unclassified_count` (unresolved weight **sets**, not
files); support contributes to payload
bytes separately. A print verdict can still have a fetch action.
The whole upstream `gguf_variants` inventory remains available even on
a selected row, with all-shard totals and exact include globs.

An archive may retain many complete variants. Bytes per parameter and
RAM estimates describe one complete model weight variant, so GGUF packs,
incomplete shard selections, and projector-only selections have neither.
The total parameter count includes inactive experts. Precision, size
scope, and preservation verdicts answer different questions.

The `redundant` hint is a separate observation from live per-dtype
parameter counts and weight bytes; it never authorizes omission.
The canonical digest fields are defined in [Catalogs](../CATALOGS.md).
