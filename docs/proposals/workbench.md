# Workbench — archives that run, on a named machine, on existing standards

Status: a direction, written 2026-09-05. Nothing here is shipped except
the first stone — the `code` artifact type, the GitHub provider, and
`code_metadata.runtime_declarations` ([Code bundles](../CODE.md)).

## The ask

darsay collects models museum-grade and proves usability with an offline
run. The next step is not a bigger `run`. It is a layer that moves
archives onto a **workbench** — a place on a specific machine — configures,
tunes, or trains them there, and runs them in a real production harness.
And it does that without inventing a format the world does not already
have. darsay's longevity thesis is that formats outlive tools; a workbench
that spoke only darsay would contradict it.

## Vocabulary — three nouns, no new verbs yet

- **Bundle** — unchanged. Facts about bytes: a model, a dataset, a code
  tree. A bundle never records what another bundle is for.
- **Harness** — a code bundle that says how to run something: a compose
  file, a Dockerfile, a launcher script. A harness is *declared by what
  the tree carries* (`runtime_declarations`), never inferred as intent.
- **Bench** — a named machine, a directory on it, and a runtime there.
  `[host]` in a vault's `config.toml` already names a machine and a path
  for hashing ([far side](../FAQ.md#how-do-i-stop-mv-and-verify-reading-a-nas-back-over-the-wire));
  a bench is that plus a container runtime and an accelerator.
- **Run record** — what happened: which bundles, which harness, which
  image digest, which bench, what answered, how fast. Volatile,
  machine-local, outside exports — `hydration.json` generalized.

## The standards to lean on, and what each one buys

| Need | Standard | How darsay uses it |
|---|---|---|
| Present a model to any runtime | Hugging Face cache layout — `$HF_HOME/hub/models--org--name/snapshots/<rev>/` | A bundle's `model/` is exactly one snapshot. A bench presents it there by symlink or bind mount, and every reader of `HF_HOME` — this recipe's `start.sh`, vLLM, `transformers`, SGLang — finds it unmodified. No format invented, no bytes copied. |
| Pin the runtime | OCI image digest (`sha256:…`) | Recorded in the run record. An image can be archived byte-exactly (`skopeo copy docker://… oci-archive:…`) and verified by digest without darsay — a candidate fourth artifact type, `image`, with its own provider. |
| Declare a harness | compose-spec (`compose.yaml`) | The declaration a bench dispatches on first; a Dockerfile second; a bare root launcher last, as an opaque command the operator confirms. |
| The served contract | OpenAI-compatible HTTP API | The bench's proof of life. One chat completion against `/v1/chat/completions` is the production analogue of `darsay run`'s offline prompt; the run record keeps request, answer, and latency. |
| A machine | ssh, `[host]` | The same door `verify` / `mv` / `cp` use. A `[bench]` table adds runtime (docker / podman), accelerator, and a working directory. |
| Pin the code | git commit | The code bundle's revision. |
| Knobs | `.env` files | A recipe's `.env.sample` is the knob list; the bench renders `.env` from bundle facts (the presented model path) and operator choices, and records what it rendered. |
| Licenses | SPDX ids | Unchanged. |

## What a serving recipe is made of

The case study behind this proposal — MiaAI-Lab's launcher for
Qwen3.8-Flash-Next on one DGX Spark — is five kinds of glue in one
`start.sh`. Naming them is what makes the workbench's scope clear:

| Layer in the recipe | What it does | General or snowflake |
|---|---|---|
| Fetch | `snapshot_download` into the HF cache, then check the safetensors index | General. darsay does it better: pinned, hashed, resumable. |
| Present | resolve `$HF_HOME/hub/models--org--name/snapshots/<rev>`, fail fast if absent | General. The bench presents a bundle there; no code from the recipe is needed. |
| Runtime choice | one container image | General in form: pin an OCI digest. The image per architecture is a support table — the ENGINES registry in miniature. |
| Budget | derive the GPU budget from live memory under a host reserve; size the KV pool; cap the cgroup | General in form, snowflake in values. The arithmetic is a policy any unified-memory box needs; the reserve and the watchdog floors were learned by killing three servers on one evening, on this hardware. |
| Ops | memory watchdog, log archiving, a graceful stop so shared memory is not leaked, refusing a port a co-tenant polls | Half and half. The watchdog and the stop are patterns; the co-tenant is one machine's service. |
| Serving contract | OpenAI-compatible endpoint, reasoning parser, chat-template flags | General. The bench's proof of life. |
| Derived artifacts | a packed table and a sliced draft vocabulary, built once and cached | General. Content-keyed derived artifacts under `.runtime/`, as the quantization proposal already has. |
| Engine patches | rewrite vLLM sources inside the image at every launch: a precision dispatch, a kernel fallback, a host-side handshake for a GPU that lacks stream memory ops, FP8 KV for sparse attention | **Snowflake.** The reason the recipe exists. |

## Snowflakes are temporal

Every patch closes a gap between what the engine supported on the day and
what this model on this GPU needed. Three gaps recur with every
generation: a model architecture ahead of the engine, a hardware quirk
ahead of the engine, a precision format ahead of the kernels. The patches
are transient by construction — they land upstream or the model and the
hardware fall out of use — and the recipe's own history shows the decay:
it credits an earlier repository for one idea, reimplements it, and the
image tag it patches is itself a special build vLLM cut for this
architecture. Glue peels off in layers over time.

So there will always be snowflakes, and the archive is where they earn
their keep: a run layer that handled only the stock case would lose
exactly the models that needed the most work to stand up, which are the
ones a future reader will least be able to reconstruct.

## The harness contract

One small, fixed contract lets the bench treat the stock case and the
snowflake case the same way:

> Given a presented model path and host facts, produce a service on an
> OpenAI-compatible endpoint.

darsay's own harness satisfies it for a model a stock engine supports on
a machine where it fits — present the bundle, pick the image from the
support table, derive the budget from host facts, launch, probe, record.
A code bundle with a launcher satisfies it for the rest. Both runs are
recorded the same way. This is why the code type records what a tree
*declares* and *references* rather than trying to understand it.

## What darsay will never take on

- **Maintaining patches to engines.** That belongs to the engine
  communities. The archive keeps the patch that worked on the day,
  pinned, beside the checkpoint it served.
- **Encoding hardware quirks as rules.** They are recorded as measurements
  against a machine — what `runtime.tested_hardware` already is in seed
  form. A reserve of twenty-six gigabytes is not a fact about darsay; it
  is a fact about a DGX Spark in September, and the record says so.
- **Reading a program to learn what it loads.** The reference scan reads
  strings in files with provenance and stops. The general problem is
  undecidable and stays unsolved on purpose.

## The composition, and where it lives

A serving recipe binds three things: a checkpoint (model bundle), a tree
(code bundle), an image (digest). Each bundle's manifest stays a record of
that artifact alone. "This tree serves that checkpoint" is not a fact
about the tree's bytes; it is a binding a curator or an operator makes.
So the `serves` edge does **not** go in `lineage.parents`. It lives in the
composition record.

Two homes, probably both:

1. **A catalog entry** — shareable, curator-owned, says *these belong
   together*: a row whose source is the code bundle and whose companions
   are the model bundle and the image reference. Catalogs already hold
   works by source ref and travel between people.
2. **Bench state** — machine-local, says *this actually ran*:
   `workbench.json` at the vault (or bench) level with entries
   `{harness, model, image: {ref, digest}, bench, presented_as, env, runs: […]}`.
   Not archival; excluded from exports; deletable; nothing in it is
   needed to open, verify, or export a bundle.

## What the code type already gives the bench

- `runtime_declarations.found.compose` — dispatch to compose;
  `container` — build; `shell` — a launcher to show, not to run blind.
- `env_template` — the knobs, verbatim from the tree.
- `references` — the model the code names (the primary, a `references`
  edge when exactly one resolves) and the image the template declares:
  what to present, and what to pull, before a bench run starts. A model
  reference carries no revision, which is the bench's cue that the
  vault's bundle is the pinned instance to present.
- An immutable `code/` — copied to the bench's working directory, never
  run in place, so the archive is never the thing that got edited on the
  box at 2 a.m.
- The commit SHA — so the run record says exactly which recipe ran.

## Phases

1. **Now.** Code bundles, runtime declarations, the GitHub provider.
2. **`darsay bench`.** Name a bench (`[bench]` in `config.toml`). Present
   a model bundle on it in the Hugging Face cache layout; verify it there
   through the far side. No process is started.
3. **`darsay bench run`.** Take a code bundle and a model bundle to a
   bench: copy the tree, render `.env`, start the harness (compose, then
   Dockerfile, then a confirmed launcher), probe the OpenAI-compatible
   endpoint, write the run record, stop. One bench, one harness, one run.
4. **Images.** An `image` artifact type and an OCI provider: archive the
   image by digest, so the third artifact of a recipe has a bundle too.
5. **Tune and train.** A harness whose output is a *new* model bundle —
   `archive` from a bench path with a `finetune` edge back to its input —
   closes the loop: archive → run → train → archive.

## Non-goals

- No scheduler, no fleet, no orchestration. Kubernetes is where a compose
  file goes when it grows up; darsay hands off, it does not become it.
- No invented harness format. A tree with no standard declaration runs
  nothing automatically.
- No composition fact inside a manifest. Bundles stay records of bytes.

## Open questions

- Which of the two homes is primary for the `serves` binding — the
  shareable catalog row or the machine-local run record? Leaning: the
  catalog says what belongs together, the bench records what ran.
- The reference scan surfaces the model a recipe names as evidence with
  provenance, and the one-model rule makes the edge. Should a resolving
  primary model be added to a catalog automatically when the recipe is,
  so `archive --next` fetches the pair? Leaning yes, as a want the
  curator can drop — but as a catalog rule, never a manifest fact.
- Where does a run's answer go when it is evidence a model still works
  — `runtime.tested_hardware` already exists for `darsay run`; a bench run
  is the same claim about a different engine.
