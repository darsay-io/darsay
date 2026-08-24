"""modelvault: archive full model ecosystems as reproducible, auditable bundles."""

__version__ = "0.4.0"

# Version of the manifest.json schema, independent of the tool version.
# Bump the major component on breaking layout changes; consumers should
# check this before parsing.
# 1.1.0: defined the shape of runtime.tested_hardware entries (written by
#        `modelvault run`); previously always null.
# 1.2.0: dataset artifact type (payload under data/, dataset_metadata section,
#        smoke_tests.structure, dataset relationships) and the model-side
#        relationships.training_datasets field. Additive.
SCHEMA_VERSION = "1.2.0"
