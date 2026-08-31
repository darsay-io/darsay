from __future__ import annotations

import pytest

from darsay.providers.base import FileSpec
from darsay.subset import is_sidecar, matches_include, select_subset


def test_matches_include_path_and_basename():
    assert matches_include("weights/foo.Q4_K_M.gguf", ["*Q4_K_M*"])
    assert matches_include("foo.Q4_K_M.gguf", ["*Q4_K_M*"])
    assert not matches_include("foo.Q8_0.gguf", ["*Q4_K_M*"])


def test_matches_include_anchored():
    assert matches_include("model.safetensors", ["/model.safetensors"])
    assert not matches_include("FL2VA/model.safetensors", ["/model.safetensors"])
    # Unanchored keeps the filename fallback.
    assert matches_include("FL2VA/model.safetensors", ["model.safetensors"])
    assert matches_include("a/b.gguf", ["/a/*.gguf"])
    assert not matches_include("x/a/b.gguf", ["/a/*.gguf"])


def test_is_sidecar_names():
    assert is_sidecar("config.json")
    assert is_sidecar("LICENSE")
    assert is_sidecar("README.md")
    assert is_sidecar("tokenizer.json")
    assert is_sidecar("model.safetensors.index.json")
    assert is_sidecar("pytorch_model.bin.index.json")
    assert is_sidecar("video_preprocessor_config.json")
    assert not is_sidecar("model.safetensors")
    assert not is_sidecar("index.json")
    assert not is_sidecar("Q4_K_M.gguf")
    assert not is_sidecar("extra.bin")
    assert is_sidecar("License")
    assert is_sidecar("readme.md")


def test_select_subset_keeps_matches_and_sidecars():
    files = [
        FileSpec("Q4_K_M.gguf", 100, sha256="aa"),
        FileSpec("Q8_0.gguf", 200, sha256="bb"),
        FileSpec("config.json", 10, git_sha1="cc"),
        FileSpec("LICENSE", 4, git_sha1="dd"),
        FileSpec("extra.bin", 50, sha256="ee"),
    ]
    kept, subset = select_subset(files, ["*Q4_K_M*"])
    paths = [item.path for item in kept]
    assert paths == ["LICENSE", "Q4_K_M.gguf", "config.json"]
    assert subset["include"] == ["*Q4_K_M*"]
    assert subset["sidecars"] is True
    assert subset["sidecar_file_count"] == 2
    assert subset["full_file_count"] == 5
    assert subset["kept_file_count"] == 3
    assert subset["omitted_file_count"] == 2
    assert subset["full_total_size_bytes"] == 364
    assert [item["path"] for item in subset["full_files"]] == sorted(
        item["path"] for item in subset["full_files"]
    )
    assert {item["path"] for item in subset["full_files"]} == {
        "Q4_K_M.gguf",
        "Q8_0.gguf",
        "config.json",
        "LICENSE",
        "extra.bin",
    }


def test_select_subset_without_sidecars():
    files = [
        {"path": "a.gguf", "size": 1},
        {"path": "config.json", "size": 2},
    ]
    kept, subset = select_subset(files, ["*.gguf"], sidecars=False)
    assert [item["path"] for item in kept] == ["a.gguf"]
    assert subset["sidecar_file_count"] == 0


def test_select_subset_no_match_exits():
    with pytest.raises(SystemExit, match="matched no payload files"):
        select_subset([FileSpec("a.bin", 1)], ["*gguf*"])
