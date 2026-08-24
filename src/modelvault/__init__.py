"""modelvault: archive full model ecosystems as reproducible, auditable bundles."""

__version__ = "0.5.0"

# Version of the manifest.json schema, independent of the tool version.
# Bump the major component on breaking layout changes; consumers should
# check this before parsing.
# 1.1.0: defined the shape of runtime.tested_hardware entries (written by
#        `modelvault run`); previously always null.
# 1.2.0: dataset artifact type (payload under data/, dataset_metadata section,
#        smoke_tests.structure, dataset relationships) and the model-side
#        relationships.training_datasets field. Additive.
# 1.3.0: source.access (Hub gate status at archive time); structured lineage —
#        relationships.base_models (all parents) and base_model_relation (the
#        model-tree edge label), with finetuned_from now set only when the
#        declared relation is `finetune`; licensing needs_manual_review/notes
#        are gate-aware. Additive.
# 1.4.0: source.transfer session/accounting summary; source.mirrors_used is
#        populated for verified sibling-bundle copies; archive-time checksum
#        verification records its per-file timing. Additive.
SCHEMA_VERSION = "1.4.0"
