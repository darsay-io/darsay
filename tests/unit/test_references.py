"""The reference scan: offline, deterministic, three tiers of provenance,
one rule for the edge, and the caps the record has to confess."""

from __future__ import annotations

import pytest

from darsay.references import (
    LOCAL_SCAN,
    REMOTE_SCAN,
    ScanLimits,
    classify_value,
    file_class,
    primary_edge,
    resolve_references,
    scan_references,
    select_scan_files,
)

MODEL = "huggingface:Mia-AiLab/Qwen3.8-Flash-Next-NVFP4"
IMAGE = "oci:vllm/vllm-openai:qwen38-flash-next"

RECIPE = {
    ".env.sample": b'IMAGE="vllm/vllm-openai:qwen38-flash-next"\nSERVED_MODEL_NAME="qwen3.8-flash-next"\nPORT=8888\n',
    "download.sh": (
        b"#!/usr/bin/env bash\n"
        b'MODEL_ID="${1:-${TP1_MODEL_ID:-Mia-AiLab/Qwen3.8-Flash-Next-NVFP4}}"\n'
        b'IMAGE="${IMAGE:-vllm/vllm-openai:qwen38-flash-next}"\n'
        b"ls /usr/bin /dev/shm files/patch.py 5/50/95\n"
    ),
    "start.sh": b'MODEL_ID="${TP1_MODEL_ID:-Mia-AiLab/Qwen3.8-Flash-Next-NVFP4}"\n',
    "files/build_draft_vocab.py": b'REPO = "Mia-AiLab/Qwen3.8-Flash-Next-NVFP4"\n# credit: https://github.com/lancelind/qwen3.8-Flash-DGX\n',
    "README.md": (
        b"# Recipe\n"
        b"Serves `Mia-AiLab/Qwen3.8-Flash-Next-NVFP4` (99 GB) via vllm/vllm-openai:qwen38-flash-next.\n"
        b"Credit: [lancelind/qwen3.8-Flash-DGX](https://github.com/lancelind/qwen3.8-Flash-DGX).\n"
        b"Read/write and/or input/output at 1.4 MiB/token, KV/head, 5/50/95 depth.\n"
        b"Follow https://github.com/sponsors/MiaAI-Lab and https://huggingface.co/datasets/acme/corpus.\n"
    ),
    ".github/ISSUE_TEMPLATE/1_bug.md": b"Model: Mia-AiLab/Qwen3.8-Flash-Next-NVFP4\n",
    "LICENSE": b"AGPL\n",
    "logo.png": b"\x89PNG\x00\x00binary",
}


def _scan(files=RECIPE):
    return scan_references([(p, d) for p, d in files.items()])


def _by_ref(record):
    return {item["ref"]: item for item in record["items"] or []}


def test_file_classes():
    assert file_class(".env.sample") == "env_template"
    assert file_class("deploy/compose.yaml") == "compose"
    assert file_class("docker-compose.gpu.yml") == "compose"
    assert file_class("Dockerfile") == "dockerfile"
    assert file_class("docker/Dockerfile.gpu") == "dockerfile"
    assert file_class("start.sh") == "code"
    assert file_class("files/patch.py") == "code"
    assert file_class("Makefile") == "code"
    assert file_class("README.md") == "prose"
    assert file_class("LICENSE") == "prose"
    assert file_class(".github/ISSUE_TEMPLATE/1_bug.md") == "prose"
    assert file_class("logo.png") is None
    assert file_class("model.safetensors") is None


def test_classify_value():
    assert classify_value("vllm/vllm-openai:qwen38-flash-next") == ("image", IMAGE)
    assert classify_value("python:3.12-slim") == ("image", "oci:python:3.12-slim")
    assert classify_value("ghcr.io/org/img@sha256:" + "a" * 64) == (
        "image",
        "oci:ghcr.io/org/img@sha256:" + "a" * 64,
    )
    assert classify_value('"Mia-AiLab/Qwen3.8-Flash-Next-NVFP4"') == ("model", MODEL)
    assert classify_value("https://huggingface.co/Qwen/Qwen3-0.6B/tree/main") == (
        "model",
        "huggingface:Qwen/Qwen3-0.6B",
    )
    assert classify_value("https://hf.co/datasets/acme/corpus") == (
        "dataset",
        "huggingface:datasets/acme/corpus",
    )
    assert classify_value("https://github.com/o/r.git") == ("code", "github:o/r")
    assert classify_value("https://github.com/sponsors/o") is None
    assert classify_value("qwen3.8-flash-next") is None
    assert classify_value("files/patch.py") is None
    assert classify_value("usr/bin") is None
    assert classify_value("") is None


def test_scan_finds_the_model_the_image_and_the_credit_with_provenance():
    record = _scan()
    items = _by_ref(record)
    model = items[MODEL]
    assert model["kind"] == "model"
    assert model["tier"] == "evidence"
    assert model["declared_by"] == "shell_default"
    assert model["found_in"][:2] == ["download.sh:2", "start.sh:1"]
    assert "files/build_draft_vocab.py:1" in model["found_in"]
    assert "README.md:2" in model["found_in"]
    assert ".github/ISSUE_TEMPLATE/1_bug.md:1" in model["found_in"]
    assert model["occurrences"] == 5
    assert model["revision"] is None
    assert model["resolved"] is None

    image = items[IMAGE]
    assert image["tier"] == "declared"
    assert image["declared_by"] == "env_template"
    assert image["found_in"][0] == ".env.sample:1"
    assert image["digest"] is None

    credit = items["github:lancelind/qwen3.8-Flash-DGX"]
    assert credit["kind"] == "code"
    assert credit["tier"] == "mentioned"  # a URL in a comment is prose
    assert credit["declared_by"] == "url"
    assert "huggingface:lancelind/qwen3.8-Flash-DGX" not in items

    corpus = items["huggingface:datasets/acme/corpus"]
    assert corpus["kind"] == "dataset" and corpus["tier"] == "mentioned"

    # Prose noise never becomes a reference; neither do paths or fractions.
    junk = {"and/or", "input/output", "MiB/token", "KV/head", "Read/write"}
    for ref in items:
        assert not any(j in ref for j in junk), ref
    assert not any(
        "usr/bin" in r or "dev/shm" in r or "files/patch" in r for r in items
    )
    assert not any("sponsors" in r for r in items)
    # Order: tier, then kind, then ref — deterministic for the record.
    assert [i["ref"] for i in record["items"]][:2] == [IMAGE, MODEL]
    scan = record["scan"]
    assert scan["files_scanned"] == 7
    assert scan["skipped"]["binary"] == 1
    assert scan["partial"] is False
    assert record["primary_model"] == {
        "ref": None,
        "rule": "the one model named in code that resolves upstream",
        "candidates": [MODEL],
        "reason": "not resolved upstream (offline, or the lookup failed)",
    }


def test_declarations_from_compose_dockerfile_and_spaces_card():
    files = {
        "compose.yaml": b"services:\n  serve:\n    image: ghcr.io/acme/serve:1.2\n    build: .\n",
        "Dockerfile": (
            b"FROM python:3.12-slim AS builder\n"
            b"FROM builder\n"
            b"FROM scratch\n"
            b"FROM --platform=linux/amd64 nvcr.io/nvidia/pytorch:24.01-py3\n"
        ),
        "README.md": (
            b"---\ntitle: demo\nmodels:\n  - Qwen/Qwen3-0.6B\n  - 'acme/toy'\n"
            b"datasets:\n  - acme/corpus\ntags:\n  - x\n---\n# Demo\n"
        ),
    }
    items = _by_ref(_scan(files))
    assert items["oci:ghcr.io/acme/serve:1.2"]["declared_by"] == "compose"
    assert items["oci:python:3.12-slim"]["declared_by"] == "dockerfile"
    assert items["oci:nvcr.io/nvidia/pytorch:24.01-py3"]["declared_by"] == "dockerfile"
    assert "oci:builder" not in items and "oci:scratch" not in items
    assert items["huggingface:Qwen/Qwen3-0.6B"]["declared_by"] == "spaces_card"
    assert items["huggingface:acme/toy"]["tier"] == "declared"
    assert items["huggingface:datasets/acme/corpus"]["kind"] == "dataset"
    assert all(i["tier"] == "declared" for i in items.values())


def test_quoted_literals_count_in_code_but_plain_words_do_not():
    files = {
        "app.py": b'MODEL = "google/gemma"\nother = load(google/gemma)\nx = "acme/toy-v2"\n',
        "notes.md": b"Uses `google/gemma` and google/gemma plainly.\n",
    }
    items = _by_ref(_scan(files))
    gemma = items["huggingface:google/gemma"]
    assert gemma["tier"] == "evidence"
    assert gemma["found_in"] == [
        "app.py:1",
        "notes.md:1",
    ]  # backticks count, bare prose does not
    assert gemma["occurrences"] == 2
    assert items["huggingface:acme/toy-v2"]["tier"] == "evidence"


def test_resolution_is_capped_and_recorded_and_makes_exactly_one_edge():
    record = _scan()
    asked = []

    def exists(ref):
        asked.append(ref)
        return ref == MODEL

    resolve_references(record, exists, limit=5)
    items = _by_ref(record)
    assert items[MODEL]["resolved"] is True
    assert items["github:lancelind/qwen3.8-Flash-DGX"]["resolved"] is None
    assert items[IMAGE]["resolved"] is None  # no provider for an image
    assert items["huggingface:datasets/acme/corpus"]["resolved"] is None  # a mention
    assert record["query_limit"] == 5 and record["resolved_at"]
    assert IMAGE not in asked
    assert "github:lancelind/qwen3.8-Flash-DGX" not in asked  # mentioned only
    assert record["primary_model"]["ref"] == MODEL
    assert record["primary_model"]["reason"] is None
    assert primary_edge(record) == {
        "source": MODEL,
        "relation": "references",
        "declared_by": "shell_default",
    }


def test_two_resolving_models_make_no_edge_and_say_why():
    files = {
        "a.sh": b'A="${A:-acme/one-v1}"\n',
        "b.sh": b'B="${B:-acme/two-v2}"\n',
    }
    record = _scan(files)
    resolve_references(record, lambda ref: True)
    assert record["primary_model"]["ref"] is None
    assert record["primary_model"]["candidates"] == [
        "huggingface:acme/one-v1",
        "huggingface:acme/two-v2",
    ]
    assert "2 resolving candidates" in record["primary_model"]["reason"]
    assert primary_edge(record) is None


def test_a_model_that_does_not_resolve_is_kept_but_is_not_the_primary():
    record = _scan({"a.sh": b'A="${A:-acme/gone-v1}"\n'})
    resolve_references(record, lambda ref: False)
    assert _by_ref(record)["huggingface:acme/gone-v1"]["resolved"] is False
    assert record["primary_model"]["reason"] == "none resolved upstream"


def test_lookup_failures_are_unknown_never_crashes():
    record = _scan()

    def broken(ref):
        raise RuntimeError("offline")

    resolve_references(record, broken)
    assert _by_ref(record)[MODEL]["resolved"] is None
    assert primary_edge(record) is None


def test_lookup_cap_leaves_the_rest_unknown():
    files = {f"s{i}.sh": f'X="${{X:-acme/m{i}-v1}}"\n'.encode() for i in range(5)}
    record = _scan(files)
    resolve_references(record, lambda ref: True, limit=2)
    resolved = [i["resolved"] for i in record["items"]]
    assert resolved == [True, True, None, None, None]
    assert "2 resolving candidates" in record["primary_model"]["reason"]


def test_select_scan_files_prioritizes_declarations_and_confesses_skips():
    entries = [
        ("zzz/deep/util.py", 100),
        ("README.md", 100),
        ("start.sh", 100),
        (".env.sample", 100),
        ("weights.safetensors", 10**9),
        ("huge.py", 10**7),
        ("docs/notes.md", 100),
        ("compose.yaml", 100),
    ]
    chosen, skipped = select_scan_files(entries, LOCAL_SCAN)
    assert chosen == [
        ".env.sample",
        "README.md",
        "compose.yaml",
        "start.sh",
        "zzz/deep/util.py",
        "docs/notes.md",
    ]
    assert skipped == {"not_text": 1, "too_large": 1, "over_budget": 0}
    tight = ScanLimits(file_bytes=100, total_bytes=250, file_count=10)
    chosen, skipped = select_scan_files(entries, tight)
    assert chosen == [".env.sample", "README.md"]
    assert skipped["over_budget"] == 4
    record = scan_references([], skipped=skipped)
    assert record["scan"]["partial"] is True
    assert record["items"] is None
    assert REMOTE_SCAN.file_count < LOCAL_SCAN.file_count


@pytest.mark.parametrize(
    "line",
    [
        "see /usr/local/bin/tool",
        "curl localhost:8888/v1/chat/completions",
        "-v $HF_CACHE_DIR:/hf -e HF_HOME=/hf",
        "measured 2026/09/05 at 1/2 speed",
        "tok/s and MiB/token",
        "models--Mia-AiLab--Qwen3.8-Flash-Next-NVFP4/snapshots/abc",
        "from files.patch_ple_layer import x",
    ],
)
def test_lines_that_must_not_yield_references(line):
    record = scan_references([("x.sh", line.encode()), ("x.md", line.encode())])
    assert record["items"] is None, record["items"]
