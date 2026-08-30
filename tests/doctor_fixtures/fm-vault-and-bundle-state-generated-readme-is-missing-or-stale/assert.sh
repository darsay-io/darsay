#!/bin/sh
set -eu

sandbox=$1
fixture_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
repo_dir=$(CDPATH= cd -- "$fixture_dir/../../.." && pwd)

PYTHONPATH="$repo_dir/src:$repo_dir" "${DARSAY_TEST_PYTHON:-python3}" - "$sandbox" <<'PY'
import sys
from pathlib import Path

from darsay.archiver import load_manifest
from darsay.readme_gen import render_bundle_readme

vault = Path(sys.argv[1])
manifest = next(vault.glob("*/*/manifest.json"))
bundle = manifest.parent
assert (bundle / "README.md").read_text(encoding="utf-8") == render_bundle_readme(
    bundle, load_manifest(bundle)
)
PY
