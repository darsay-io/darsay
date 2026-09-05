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
- Should `estimate` surface the model a recipe wants (`MODEL_ID` in
  `.env.sample`)? It is a fact in the payload, but reading it is a shell
  parse. Leave it to the curator until a compose file states it as an
  `image:` or a `volumes:` entry.
- Where does a run's answer go when it is evidence a model still works
  — `runtime.tested_hardware` already exists for `darsay run`; a bench run
  is the same claim about a different engine.
