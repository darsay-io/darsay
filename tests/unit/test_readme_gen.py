from __future__ import annotations

from modelvault.readme_gen import human_params, human_size


def test_human_size():
    assert human_size(None) == "?"
    assert human_size(0) == "0 B"
    assert human_size(512) == "512 B"
    assert human_size(1024) == "1.0 KiB"
    assert human_size(1024**3) == "1.0 GiB"


def test_human_params():
    assert human_params(None) == "unknown"
    assert human_params(128) == "128"
    assert human_params(1_500_000) == "1.5M"
    assert human_params(600_000_000) == "600.0M"
    assert human_params(1_200_000_000) == "1.20B"
