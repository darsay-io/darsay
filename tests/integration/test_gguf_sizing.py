"""Real Hub inventory through the estimate/catalog boundary, without weights."""

import json
from pathlib import Path

from darsay.catalog import estimate_digest
from darsay.estimate import estimate, print_estimate
from darsay.providers.base import FileSpec, Snapshot
from darsay.providers.huggingface import HuggingFaceProvider


def test_glm_inventory_and_selected_variant_have_distinct_prices(monkeypatch, vault):
    fixture = json.loads(
        (Path(__file__).parents[1] / "fixtures/glm-5.3-flash-gguf.json").read_text()
    )
    provider = HuggingFaceProvider()
    ref = provider.parse(fixture["source"])
    snapshot = Snapshot(
        source=ref,
        revision=fixture["revision"],
        revision_ref="main",
        files=[FileSpec(**f) for f in fixture["files"]],
        metadata={"gguf": {"architecture": fixture["architecture"]}},
        parameters={
            "total": fixture["parameters"],
            "by_dtype": None,
            "dominant_dtype": None,
            "source": "gguf",
        },
    )
    monkeypatch.setattr(provider, "pin", lambda *a, **kw: snapshot)
    monkeypatch.setattr("darsay.estimate.get_provider", lambda _: provider)
    whole = estimate(ref, vault=vault, full=True, progress=lambda _: None)
    digest = estimate_digest(whole)
    assert digest["size_basis"] == "repository"
    assert digest["payload_bytes"] == digest["repository_bytes"] == 2_545_636_747_545
    assert digest["parameters"] == 320_759_404_382
    assert digest["parameters_source"] == "gguf"
    assert digest["bytes_per_param"] is None
    assert whole["estimates"]["min_ram_gb"] is None
    assert len(digest["gguf_variants"]) == 12
    choice = next(v for v in digest["gguf_variants"] if v["precision"] == "UD-Q4_K_XL")
    selected = estimate(
        ref, vault=vault, include=choice["include"], progress=lambda _: None
    )
    priced = estimate_digest(selected)
    assert priced["size_basis"] == "selection"
    assert priced["payload_bytes"] == 199_707_321_347 + 8_377  # README is retained
    assert priced["repository_bytes"] == digest["repository_bytes"]
    assert priced["precision"] == "UD-Q4_K_XL"
    assert priced["bytes_per_param"] == 0.623
    assert priced["classification"] is None
    lines = []
    print_estimate(whole, lines.append)
    report = "\n".join(lines)
    assert "upstream gguf metadata" in report
    assert "12 model weight variant(s)" in report
    assert "BF16: 597.6 GiB in 14 files" in report
    assert "UD-Q4_K_XL: 186.0 GiB in 6 files" in report
    assert "no higher-fidelity public copy" not in report


def test_projector_does_not_change_model_precision(monkeypatch, vault):
    provider = HuggingFaceProvider()
    ref = provider.parse("acme/vision")
    snapshot = Snapshot(
        source=ref,
        revision="a" * 40,
        revision_ref="main",
        metadata={},
        files=[FileSpec("model-Q4_K_M.gguf", 400), FileSpec("mmproj-F16.gguf", 200)],
        parameters={
            "total": 1000,
            "by_dtype": None,
            "dominant_dtype": None,
            "source": "gguf",
        },
    )
    monkeypatch.setattr(provider, "pin", lambda *a, **kw: snapshot)
    monkeypatch.setattr("darsay.estimate.get_provider", lambda _: provider)
    priced = estimate_digest(
        estimate(ref, vault=vault, full=True, progress=lambda _: None)
    )
    assert priced["precision"] == "Q4_K_M"
    assert priced["bytes_per_param"] == 0.4
    assert priced["payload_bytes"] == 600
    assert len(priced["gguf_variants"]) == 1
