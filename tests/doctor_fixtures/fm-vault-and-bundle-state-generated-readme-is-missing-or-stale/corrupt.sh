#!/bin/sh
set -eu

sandbox=$1
fixture_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
repo_dir=$(CDPATH= cd -- "$fixture_dir/../../.." && pwd)

PYTHONPATH="$repo_dir/src:$repo_dir" "${DARSAY_TEST_PYTHON:-python3}" \
  "$fixture_dir/create.py" "$sandbox"

readme=$(find "$sandbox/test--acme--toy" -mindepth 2 -maxdepth 2 -name README.md -print -quit)
[ -n "$readme" ]
printf '%s\n' 'operator accidentally edited generated output' > "$readme"

mkdir -p "$sandbox/.fixture_baseline"
cp -a "$sandbox/test--acme--toy" "$sandbox/.fixture_baseline/"
