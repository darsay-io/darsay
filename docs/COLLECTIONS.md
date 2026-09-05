# Choose your collection

> **In one sentence.** Inspect a publication, choose which encodings and
> companions matter to you, and confirm a pinned collection before storing it.

A repository is a publication, not necessarily one model copy. A GGUF pack
can contain a dozen alternative encodings, each split across several files.
The collection room makes those alternatives visible before you commit to
their storage. It is available on the board and in an interactive terminal.

## Start with an intention

Nothing is selected until you choose a starting point or a variant yourself.
The three starting points are explicit, editable choices:

| Intention | What it selects | What it does not claim |
| --- | --- | --- |
| One considered copy | The smallest complete, known-size 4-bit variant in this publication | Best answers, publisher recommendation, runtime support, or hardware fit |
| A comparison pair | The smallest complete, known-size 4-bit variant and its 8-bit counterpart | A benchmark result or a guarantee that the larger encoding is better |
| The whole publication | The repository at the inspected revision, with no selectors | That every group is complete, original, or impossible to recreate |

There is no guessed substitute if a requested family is missing. Choose
another variant manually. An unknown-size variant can be selected, but it
cannot serve as a size-ranked starting point. Missing or repeated shards
prevent a group from being selected as a complete variant; the whole
publication can still preserve the inventory as found.

Four-bit is an encoding family, not an exact file-size formula. Scales,
metadata, and mixed-precision tensors also take space. Wider storage is
not evidence of an original checkpoint. Use the publisher's model card,
model-specific evaluations, and your runtime's requirements to judge a
variant's suitability. darsay does not infer those facts from its name.

## In the terminal

```bash
darsay archive unsloth/GLM-5.3-Flash-GGUF
```

For a fresh model with multiple GGUF variants, an interactive terminal
opens the collection room after resolving the revision and before creating
an archive directory or transfer ledger. It reads metadata, not model bytes.

| Key | Action |
| --- | --- |
| `1`, `2`, `3` | Apply the three starting points |
| Arrow keys or `j` / `k` | Move among variants and companions |
| Space | Toggle a complete group, including all its shards |
| `?` | Open the focused variant's field guide; arrow keys scroll it |
| Enter | Review the collection; Enter again confirms the scope |
| Escape | Return from the guide or review; cancel from selection |
| `q` or Ctrl-C | Cancel without starting an archive |

The bottom readout updates the selected disk bytes, file count, and share
of the publication. An ordinary 80×24 terminal shows eight groups at a time
and says how many more lie above or below; messages wrap rather than clip.
At 108 columns the guide sits beside the inventory and says when `?` holds
more; narrower terminals keep it under `?`. The minimum is 60 columns by 20
rows. Resizing preserves the draft. Cancelling restores the terminal.

Confirmation continues to the ordinary archive plan and disk preflight.
With `--dry-run`, it writes the chosen pin and plan but transfers no payload.
Cancellation before confirmation writes neither. Rerunning a direct-source
archive resumes its recorded collection without reopening the picker or
expanding it. `--full` cannot silently widen an existing subset.

### Explicit commands and unattended work

`--include` and `--full` already state your scope, so they bypass the picker.
So do board jobs, `--next`, shards, and resumed pins. A board or catalog job
must match its row's source, revision, and includes; it is not a new curation
session. Non-interactive input/output and `TERM=dumb` never open a TUI.
`--include '/*'` states the whole repository and means exactly what an
unqualified source means; it is not a subset.

`--yes` skips both the picker and disk-preflight confirmation. On a fresh
unqualified source it uses the default conservative archive policy, which
can retain an entire GGUF pack. It does **not** select the 4-bit starting point.
For reproducible automation, specify the revision and scope:

```bash
darsay archive unsloth/GLM-5.3-Flash-GGUF \
  --revision 2975ab414d30340466d8c51533c6e91f0cca64c1 \
  --include '/UD-Q4_K_XL/GLM-5.3-Flash-UD-Q4_K_XL-*-of-00006.gguf'
```

This selection's six GGUF shards and README total **199,707,329,724 bytes
(186.0 GiB)** at that revision. It includes no projector. These are disk
bytes, not a RAM or VRAM requirement.

## On the board

Enter a Hub source and choose **Explore collection**. The dialog reads
the inventory without adding a row. Choose a starting point or check the
variants directly. The field notes explain each encoding and why someone
might collect it; on a phone they open beneath the variant.

**Review collection** shows the selected groups, companions, total files,
known bytes, and exact commit. **Add this collection** saves one row with
the combined selectors. The row pins the inspected revision, so a moving
branch cannot change the meaning of the reviewed selection. Adding a row
downloads no model payload. Closing or cancelling leaves the form intact.

If inspection fails, you can retry or explicitly choose **Keep an uninspected
publication**. Its separate review says **Size unknown**, requests the whole
repository with no selectors, and shows the requested but unresolved revision.
It does not pretend a variant or storage budget was verified. No fallback is
automatic.

Whole-publication scope is the repository itself. The row or pin carries no
include selectors, and `/*` typed by hand means the same thing, so it shares
the identity, size basis, and resume behaviour of an unqualified
`darsay archive SOURCE`. What the archive retains inside that scope is a
separate, recorded decision: by default only hash-identical duplicates within
the bundle are skipped, and `--full` keeps those too. In a narrower collection,
recognized support files accompany the selected groups and are counted once.
Other files are outside the scope unless explicitly selected. Toggling a group
after choosing the whole publication switches to selected-group scope; check
the new total.

An existing exact selection is labeled **Already on this board**. A final
revision-checked save protects another curator's intervening changes. The
existing row's **Add variant** action remains useful for a separate collection
row; it does not edit the original publication row.

One source and revision occupy one collection in a vault. Combine variants
that you intend to archive together, or use separate vaults for independent
collections of the same publication. An explicit request that differs from
an existing pin is refused, not silently replaced with that pin's files.
`--force` deliberately re-pins, including a partial collection; it is not
ordinary resume. Before it re-pins, it lists every payload file on disk that
the new scope leaves out, with its size, and an interactive run asks before
removing them; a dry run lists them and removes nothing. The room opens with
the existing pin selected and says what a different choice discards. Use
another vault if you want to keep both independent collections.

## Companions and context

Projectors are separate components, not smaller versions of the language
model. Multimodal use may require a matching projector; do not choose one
by precision alone. The picker never infers that compatibility. You can
collect several companions for preservation, but that does not make them
interchangeable at runtime.

Recognized support includes model cards, licenses, tokenizers, configuration,
indexes, and code. Those files accompany every subset once. This is a
documented file-selection rule, not proof that a loader has everything it
needs. Multiple model variants make one archive collection, not one runnable
model. Preparing a runtime is a separate, compatibility-checked step.

## Recreation difficulty is an evidence question

The picker says **Exact recovery unverified**. An inventory and a precision
label do not reveal whether recreating these exact bytes is easy, costly, or
impossible. That needs pinned source weights, tools, settings, any calibration
inputs, and output hashes that actually match. A working conversion or a
declared parent is not a verified reconstruction. No recreation-cost estimate
is invented.

Choosing less means “outside this collection,” not “safe to throw away.”
Collection scope, artifact identity and lineage, recovery evidence, and the
retain/omit decision remain separate. See [Quantization](QUANTIZATION.md)
and the [North star](NORTH-STAR.md).
