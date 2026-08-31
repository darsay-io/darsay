"""Classification rules R1-R14, the set model, and selection synthesis."""

from __future__ import annotations

from darsay.classify import (
    attach_selection,
    build_sets,
    classify_source,
    evaluate,
    set_glob,
)
from tests.fakes import TestProvider as FakeProvider
from tests.payloads import make_gguf, make_safetensors


def F(path, size=100, sha256=None):
    return {"path": path, "size": size, "sha256": sha256}


def _facts(**overrides):
    facts = {
        "configs": {".": {"torch_dtype": "bfloat16"}},
        "indexes": {},
        "gguf": {},
        "st_headers": {},
        "base": {"locator": None, "sha256": {}, "in_vault": False, "error": None},
    }
    facts.update(overrides)
    return facts


def _by_rule(result):
    return {s["rule"]: s for s in result["sets"]}


def _set_for(result, path):
    for s in result["sets"]:
        if path in s["paths"]:
            return s
    raise AssertionError(f"no set holds {path}")


# ---------------------------------------------------------------- set model


def test_build_sets_indexed_orphan_and_support():
    files = [
        F("model-00001-of-00002.safetensors"),
        F("model-00002-of-00002.safetensors"),
        F("stray-00001-of-00018.safetensors"),
        F("model.safetensors.index.json", 10),
        F("config.json", 10),
        F("Q4_K_M.gguf"),
    ]
    indexes = {
        "model.safetensors.index.json": {
            "weight_map": {
                "a": "model-00001-of-00002.safetensors",
                "b": "model-00002-of-00002.safetensors",
            }
        }
    }
    kinds = {s["kind"]: s for s in build_sets(files, indexes)}
    assert kinds["indexed"]["paths"] == [
        "model-00001-of-00002.safetensors",
        "model-00002-of-00002.safetensors",
    ]
    assert kinds["orphan"]["paths"] == ["stray-00001-of-00018.safetensors"]
    assert kinds["gguf"]["paths"] == ["Q4_K_M.gguf"]
    assert set(kinds["support"]["paths"]) == {
        "model.safetensors.index.json",
        "config.json",
    }


def test_build_sets_no_index_means_standalone_not_orphan():
    files = [F("unet/diffusion_model.safetensors"), F("unet/config.json", 10)]
    kinds = {s["kind"] for s in build_sets(files, {})}
    assert "orphan" not in kinds
    assert "standalone" in kinds


def test_build_sets_unreadable_index_taints_only_its_directory():
    files = [F("model.safetensors"), F("sub/model.safetensors")]
    indexes = {"model.safetensors.index.json": {"__error__": "bad json"}}
    files.append(F("model.safetensors.index.json", 10))
    sets = {s["paths"][0]: s for s in build_sets(files, indexes)}
    assert sets["model.safetensors"]["kind"] == "index_error"
    assert sets["sub/model.safetensors"]["kind"] == "standalone"


def test_set_glob_shards_single_and_irregular():
    assert (
        set_glob(
            {
                "dir": ".",
                "paths": [
                    "m-00001-of-00028.safetensors",
                    "m-00002-of-00028.safetensors",
                ],
            }
        )
        == "m-*-of-00028.safetensors"
    )
    assert set_glob({"dir": ".", "paths": ["model.safetensors"]}) == (
        "model.safetensors"
    )
    assert set_glob({"dir": ".", "paths": ["a.safetensors", "b.safetensors"]}) is None


# ------------------------------------------------------------------- rules


def test_r1_support_kept_always():
    result = evaluate([F("README.md"), F("model.safetensors")], _facts(), locator="a/t")
    support = _by_rule(result)["R1"]
    assert support["verdict"] == "support"
    assert support["action"] == "fetch"


def test_r2_base_identical_skips_only_when_base_in_vault():
    files = [
        F("copy/base.safetensors", sha256="aa"),
        F("model.safetensors", sha256="bb"),
    ]
    base = {
        "locator": "acme/base",
        "sha256": {"aa": "base.safetensors"},
        "in_vault": False,
        "error": None,
    }
    result = evaluate(files, _facts(base=base), locator="a/t")
    copy_set = _set_for(result, "copy/base.safetensors")
    assert (copy_set["verdict"], copy_set["rule"]) == ("print", "R2")
    assert copy_set["action"] == "fetch"  # base not archived here
    base["in_vault"] = True
    result = evaluate(files, _facts(base=base), locator="a/t")
    assert _set_for(result, "copy/base.safetensors")["action"] == "skip"


def test_r3_indexed_full_fidelity_master():
    files = [
        F("model-00001-of-00002.safetensors"),
        F("model-00002-of-00002.safetensors"),
        F("model.safetensors.index.json", 10),
    ]
    indexes = {
        "model.safetensors.index.json": {
            "weight_map": {
                "a": "model-00001-of-00002.safetensors",
                "b": "model-00002-of-00002.safetensors",
            }
        }
    }
    result = evaluate(files, _facts(indexes=indexes), locator="a/t")
    indexed = _by_rule(result)["R3"]
    assert indexed["verdict"] == "master"
    assert indexed["action"] == "fetch"
    assert "BF16" in indexed["reason"]


def test_r4_standalone_master_and_r14_without_dtype():
    result = evaluate([F("model.safetensors")], _facts(), locator="a/t")
    assert _by_rule(result)["R4"]["verdict"] == "master"
    result = evaluate(
        [F("model.safetensors")], _facts(configs={".": None}), locator="a/t"
    )
    unknown = _by_rule(result)["R14"]
    assert unknown["verdict"] == "unknown"
    assert "dtype" in unknown["reason"]


def test_r5_quantized_safetensors_master():
    cfg = {"torch_dtype": "float16", "quantization_config": {"quant_method": "awq"}}
    result = evaluate(
        [F("model.safetensors")], _facts(configs={".": cfg}), locator="a/t"
    )
    quant = _by_rule(result)["R5"]
    assert quant["verdict"] == "master"
    assert "awq" in quant["reason"]
    mlx = {"quantization": {"bits": 4}}
    result = evaluate(
        [F("model.safetensors")], _facts(configs={".": mlx}), locator="a/t"
    )
    assert "mlx" in _by_rule(result)["R5"]["reason"]


def test_r6_orphan_unknown_kept():
    files = [
        F("model.safetensors"),
        F("stray.safetensors"),
        F("model.safetensors.index.json", 10),
    ]
    indexes = {
        "model.safetensors.index.json": {"weight_map": {"w": "model.safetensors"}}
    }
    result = evaluate(files, _facts(indexes=indexes), locator="a/t")
    orphan = _by_rule(result)["R6"]
    assert orphan["paths"] == ["stray.safetensors"]
    assert orphan["verdict"] == "unknown"
    assert orphan["action"] == "fetch"


def test_r7_imatrix_gguf_master():
    gguf = {"Q4.gguf": {"kv": {"quantize.imatrix.file": "im.dat"}}}
    result = evaluate([F("Q4.gguf")], _facts(gguf=gguf), locator="a/t")
    assert _by_rule(result)["R7"]["verdict"] == "master"


def test_r8_external_source_claim_unknown():
    gguf = {
        "Q4.gguf": {"kv": {"general.source.huggingface.repository": "someone/else"}}
    }
    files = [F("model.safetensors"), F("Q4.gguf")]
    result = evaluate(files, _facts(gguf=gguf), locator="acme/toy")
    external = _by_rule(result)["R8"]
    assert external["verdict"] == "unknown"
    assert "someone/else" in external["reason"]


def test_r9_single_candidate_print_skipped():
    gguf = {"Q4.gguf": {"kv": {"general.file_type": 15}}}
    files = [F("model.safetensors"), F("Q4.gguf")]
    result = evaluate(files, _facts(gguf=gguf), locator="acme/toy")
    print_set = _by_rule(result)["R9"]
    assert (print_set["verdict"], print_set["action"]) == ("print", "skip")
    assert result["candidates"] == 1
    # A matching source claim does not downgrade.
    gguf = {
        "Q4.gguf": {
            "kv": {
                "general.source.huggingface.repository": "https://huggingface.co/acme/toy"
            }
        }
    }
    result = evaluate(files, _facts(gguf=gguf), locator="acme/toy")
    assert _by_rule(result)["R9"]["action"] == "skip"


def test_r10_pure_quant_pack_all_master():
    gguf = {"Q4.gguf": {"kv": {}}, "Q8.gguf": {"kv": {}}}
    files = [F("Q4.gguf"), F("Q8.gguf")]
    result = evaluate(files, _facts(configs={}, gguf=gguf), locator="a/t")
    assert all(s["rule"] == "R10" for s in result["sets"] if s["kind"] == "gguf")
    assert result["skip"]["bytes"] == 0


def test_r11_two_candidates_unknown_case_study_shape():
    files = [
        F("model-00001-of-00002.safetensors"),
        F("model-00002-of-00002.safetensors"),
        F("stray-00001-of-00002.safetensors"),
        F("stray-00002-of-00002.safetensors"),
        F("model.safetensors.index.json", 10),
        F("Q4.gguf"),
    ]
    indexes = {
        "model.safetensors.index.json": {
            "weight_map": {
                "a": "model-00001-of-00002.safetensors",
                "b": "model-00002-of-00002.safetensors",
            }
        }
    }
    gguf = {"Q4.gguf": {"kv": {"general.name": "s99-merged-fixed"}}}
    result = evaluate(files, _facts(indexes=indexes, gguf=gguf), locator="a/t")
    by_rule = _by_rule(result)
    assert by_rule["R3"]["verdict"] == "master"
    assert by_rule["R6"]["verdict"] == "unknown"
    ambiguous = by_rule["R11"]
    assert ambiguous["verdict"] == "unknown"
    assert "2 candidate" in ambiguous["reason"]
    assert result["skip"]["bytes"] == 0
    assert result.get("selection") is None


def test_r11_uncertain_source_blocks_r9_and_r10():
    # The lone weight set cannot be established (no config, no header):
    # neither "sole source" nor "no source" may be claimed.
    gguf = {"Q4.gguf": {"kv": {}}}
    files = [F("model.safetensors"), F("Q4.gguf")]
    result = evaluate(files, _facts(configs={".": None}, gguf=gguf), locator="a/t")
    ambiguous = _set_for(result, "Q4.gguf")
    assert (ambiguous["verdict"], ambiguous["rule"]) == ("unknown", "R11")
    assert result["uncertain_sources"] == 1


def test_r12_r13_legacy_weights():
    files = [F("pytorch_model.bin"), F("model.safetensors")]
    result = evaluate(files, _facts(), locator="a/t")
    assert _by_rule(result)["R12"]["verdict"] == "unknown"
    result = evaluate([F("pytorch_model.bin")], _facts(), locator="a/t")
    assert _by_rule(result)["R13"]["verdict"] == "master"


def test_r14_unreadable_headers_fetch():
    gguf = {"Q4.gguf": {"__error__": "connection reset"}}
    cfg = {".": {"__error__": "gated"}}
    files = [F("model.safetensors"), F("Q4.gguf")]
    result = evaluate(files, _facts(configs=cfg, gguf=gguf), locator="a/t")
    for s in result["sets"]:
        if s["kind"] != "support":
            assert s["verdict"] == "unknown"
            assert s["action"] == "fetch"
    assert result["unclassified_count"] == 2


# --------------------------------------------------------------- selection


def _classified(files, facts, locator="acme/toy"):
    result = evaluate(files, facts, locator=locator)
    attach_selection(result, files)
    return result


def test_selection_none_without_skips():
    result = _classified([F("model.safetensors")], _facts())
    assert result["selection"] is None


def test_selection_globs_verified():
    files = [
        F("model-00001-of-00002.safetensors"),
        F("model-00002-of-00002.safetensors"),
        F("model.safetensors.index.json", 10),
        F("config.json", 10),
        F("Q4.gguf"),
    ]
    indexes = {
        "model.safetensors.index.json": {
            "weight_map": {
                "a": "model-00001-of-00002.safetensors",
                "b": "model-00002-of-00002.safetensors",
            }
        }
    }
    gguf = {"Q4.gguf": {"kv": {}}}
    result = _classified(files, _facts(indexes=indexes, gguf=gguf))
    assert result["selection"] == {
        "include": ["model-*-of-00002.safetensors"],
        "explicit_paths": False,
    }
    assert result["skip"]["files"] == 1


def test_selection_anchors_past_basename_collisions():
    # The kept root file's basename also names the skipped twin; a
    # root-anchored pattern tells them apart.
    files = [
        F("model.safetensors", sha256="bb"),
        F("copy/model.safetensors", sha256="aa"),
    ]
    base = {
        "locator": "acme/base",
        "sha256": {"aa": "model.safetensors"},
        "in_vault": True,
        "error": None,
    }
    result = _classified(files, _facts(base=base))
    assert result["selection"]["include"] == ["/model.safetensors"]
    assert result["skip"]["files"] == 1


def test_selection_degrades_to_none_when_unverifiable():
    # Glob metacharacters in a kept path defeat fnmatch at every tier —
    # the honest outcome is the full fetch.
    files = [
        F("we[i]rd.safetensors", sha256="bb"),
        F("copy/we[i]rd.safetensors", sha256="aa"),
    ]
    base = {
        "locator": "acme/base",
        "sha256": {"aa": "we[i]rd.safetensors"},
        "in_vault": True,
        "error": None,
    }
    result = _classified(files, _facts(base=base))
    assert result["selection"] is None
    assert any("could not be verified" in note for note in result["notes"])


def test_selection_none_when_everything_would_skip():
    files = [F("model.safetensors", sha256="aa")]
    base = {
        "locator": "acme/base",
        "sha256": {"aa": "model.safetensors"},
        "in_vault": True,
        "error": None,
    }
    result = _classified(files, _facts(base=base))
    assert result["selection"] is None
    assert any("full repo" in note for note in result["notes"])


# ---------------------------------------------------------- fact gathering


def test_classify_source_end_to_end_hermetic():
    provider = FakeProvider()
    provider.add_repo(
        "acme/toy",
        {
            "config.json": b'{"torch_dtype": "bfloat16"}',
            "model.safetensors": make_safetensors({"w": ("BF16", [2, 2])}),
            "Q4_K_M.gguf": make_gguf({"general.file_type": 15}),
            "README.md": b"# toy\n",
        },
    )
    ref = provider.parse("acme/toy")
    snapshot = provider.pin(ref, None)
    files = [
        {"path": f.path, "size": f.size, "sha256": f.sha256} for f in snapshot.files
    ]
    result = classify_source(provider, ref, snapshot.revision, files)
    gguf_set = _set_for(result, "Q4_K_M.gguf")
    assert (gguf_set["rule"], gguf_set["action"]) == ("R9", "skip")
    assert result["selection"]["include"] == ["model.safetensors"]
    receipt = result["read"]
    assert receipt["requests"] >= 2
    assert receipt["bytes_fetched"] > 0
    assert receipt["caps"]["header_file_cap"] == 64
    # config.json carried the dtype; no safetensors header read was needed.
    assert provider.reads and all(r[0] != "model.safetensors" for r in provider.reads)


def test_classify_source_header_fallback_without_torch_dtype():
    provider = FakeProvider()
    provider.add_repo(
        "acme/toy",
        {
            "config.json": b"{}",
            "model.safetensors": make_safetensors({"w": ("BF16", [2, 2])}),
        },
    )
    ref = provider.parse("acme/toy")
    snapshot = provider.pin(ref, None)
    files = [
        {"path": f.path, "size": f.size, "sha256": f.sha256} for f in snapshot.files
    ]
    result = classify_source(provider, ref, snapshot.revision, files)
    assert _set_for(result, "model.safetensors")["rule"] == "R4"
    assert any(r[0] == "model.safetensors" for r in provider.reads)


def test_classify_source_read_failures_degrade_to_fetch():
    provider = FakeProvider()
    payload = {
        "config.json": b'{"torch_dtype": "bfloat16"}',
        "model.safetensors": make_safetensors({"w": ("BF16", [2, 2])}),
        "Q4_K_M.gguf": make_gguf({"general.file_type": 15}),
    }
    provider.add_repo("acme/toy", payload)
    from darsay.providers.base import SourceError

    provider.fail_next_read("Q4_K_M.gguf", SourceError("error: reset"))
    ref = provider.parse("acme/toy")
    snapshot = provider.pin(ref, None)
    files = [
        {"path": f.path, "size": f.size, "sha256": f.sha256} for f in snapshot.files
    ]
    result = classify_source(provider, ref, snapshot.revision, files)
    gguf_set = _set_for(result, "Q4_K_M.gguf")
    assert (gguf_set["verdict"], gguf_set["action"]) == ("unknown", "fetch")
    assert result["selection"] is None  # nothing skippable remains


def test_classify_source_base_identity_via_pin():
    provider = FakeProvider()
    shared = make_safetensors({"w": ("BF16", [2, 2])})
    provider.add_repo("acme/base", {"model.safetensors": shared})
    provider.add_repo(
        "acme/copy",
        {
            "config.json": b'{"torch_dtype": "bfloat16"}',
            "copyof/base_model.safetensors": shared,
            "model.safetensors": make_safetensors({"w": ("BF16", [3, 3])}),
        },
    )
    ref = provider.parse("acme/copy")
    snapshot = provider.pin(ref, None)
    files = [
        {"path": f.path, "size": f.size, "sha256": f.sha256} for f in snapshot.files
    ]
    result = classify_source(
        provider,
        ref,
        snapshot.revision,
        files,
        base_locator="acme/base",
        base_in_vault=True,
    )
    copy_set = _set_for(result, "copyof/base_model.safetensors")
    assert (copy_set["rule"], copy_set["action"]) == ("R2", "skip")
    assert result["read"]["base_pinned"] is True
    assert result["selection"] == {
        "include": ["model.safetensors"],
        "explicit_paths": False,
    }


def test_collect_facts_ticks_every_read(monkeypatch):
    provider = FakeProvider()
    provider.add_repo(
        "acme/toy",
        {
            "config.json": b'{"torch_dtype": "bfloat16"}',
            "model.safetensors": make_safetensors({"w": ("BF16", [2, 2])}),
            "Q4_K_M.gguf": make_gguf({"general.file_type": 15}),
            "Q8_0.gguf": make_gguf({"general.file_type": 7}),
        },
    )
    ref = provider.parse("acme/toy")
    snapshot = provider.pin(ref, None)
    files = [
        {"path": f.path, "size": f.size, "sha256": f.sha256} for f in snapshot.files
    ]
    ticks = []
    result = classify_source(
        provider, ref, snapshot.revision, files, on_read=lambda *a: ticks.append(a)
    )
    # One tick per range request, including the concurrent header reads.
    assert len(ticks) == result["read"]["requests"]
    assert result["read"]["requests"] >= 3


def test_r15_intra_repo_duplicates_skip_and_keep_shallowest():
    files = [
        F("text_encoder/model-00001-of-00002.safetensors", sha256="t1"),
        F("text_encoder/model-00002-of-00002.safetensors", sha256="t2"),
        F("FL2VA/text_encoder/model-00001-of-00002.safetensors", sha256="t1"),
        F("FL2VA/text_encoder/model-00002-of-00002.safetensors", sha256="t2"),
        F("Ref2VA/text_encoder/model-00001-of-00002.safetensors", sha256="t1"),
        F("Ref2VA/text_encoder/model-00002-of-00002.safetensors", sha256="t2"),
        F("transformer/model.safetensors", sha256="unique"),
    ]
    cfg = {"torch_dtype": "bfloat16"}
    facts = _facts(
        configs={
            "text_encoder": cfg,
            "FL2VA/text_encoder": cfg,
            "Ref2VA/text_encoder": cfg,
            "transformer": cfg,
        }
    )
    result = evaluate(files, facts, locator="a/t")
    attach_selection(result, files)
    root = _set_for(result, "text_encoder/model-00001-of-00002.safetensors")
    assert (root["verdict"], root["action"]) == ("master", "fetch")
    for prefix in ("FL2VA", "Ref2VA"):
        twin = _set_for(
            result, f"{prefix}/text_encoder/model-00001-of-00002.safetensors"
        )
        assert (twin["verdict"], twin["rule"], twin["action"]) == (
            "print",
            "R15",
            "skip",
        )
        assert twin["evidence"]["exact"] is True
    assert _set_for(result, "transformer/model.safetensors")["action"] == "fetch"
    kept = set()
    for pattern in result["selection"]["include"]:
        kept.add(pattern)
    assert not any(p.startswith(("FL2VA", "Ref2VA")) for p in kept)


def test_r15_never_claims_on_missing_hashes_or_different_bytes():
    files = [
        F("a/model.safetensors", sha256="x"),
        F("b/model.safetensors", sha256=None),
        F("c/model.safetensors", sha256="y"),
    ]
    cfg = {"torch_dtype": "bfloat16"}
    facts = _facts(configs={"a": cfg, "b": cfg, "c": cfg})
    result = evaluate(files, facts, locator="a/t")
    assert all(s["rule"] != "R15" for s in result["sets"] if s["kind"] != "support")
    assert result["skip"]["bytes"] == 0


def test_r15_duplicates_do_not_inflate_gguf_ambiguity():
    # Two byte-identical BF16 sets are one source: the GGUF stays R9.
    files = [
        F("model.safetensors", sha256="s"),
        F("mirror/model.safetensors", sha256="s"),
        F("Q4.gguf"),
    ]
    cfg = {"torch_dtype": "bfloat16"}
    gguf = {"Q4.gguf": {"kv": {"general.file_type": 15}}}
    facts = _facts(configs={".": cfg, "mirror": cfg}, gguf=gguf)
    result = evaluate(files, facts, locator="a/t")
    assert result["candidates"] == 1
    assert _set_for(result, "Q4.gguf")["rule"] == "R9"
    assert _set_for(result, "mirror/model.safetensors")["rule"] == "R15"
