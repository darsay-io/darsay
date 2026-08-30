from __future__ import annotations

from darsay.estimate import _download_lines, _format_breakdown


def test_format_breakdown_sorts_by_size():
    files = [
        {"path": "a.jsonl", "size": 10},
        {"path": "b.jsonl", "size": 5},
        {"path": "c.parquet", "size": 100},
        {"path": "README", "size": 1},
    ]
    breakdown = _format_breakdown(files)
    assert list(breakdown)[0] == "parquet"
    assert breakdown["jsonl"] == {"file_count": 2, "total_size_bytes": 15}
    assert breakdown["(none)"]["file_count"] == 1


def _est(transfer=None, total=8 * 1024**3, count=4, revision="e" * 40):
    return {
        "payload": {"total_size_bytes": total, "file_count": count},
        "source": {"revision": revision},
        "transfer": transfer,
    }


def _transfer(
    *,
    total=8 * 1024**3,
    verified=(0, 0),
    unverified=(0, 0),
    moved=(0, 0),
    partial=(0, 0),
    missing=(0, 0),
    status="in_progress",
    has_ledger=True,
    pinned=None,
):
    counts = {
        "verified": verified[0],
        "unverified": unverified[0],
        "moved": moved[0],
        "partial": partial[0],
        "missing": missing[0],
    }
    sizes = {
        "verified": verified[1],
        "unverified": unverified[1],
        "moved": moved[1],
        "partial": partial[1],
        "missing": missing[1],
    }
    banked = sizes["verified"] + sizes["unverified"] + sizes["partial"]
    return {
        "status": status,
        "resume_dir": "/vault/x/y",
        "has_ledger": has_ledger,
        "pinned_revision": pinned,
        "pinned_revision_ref": "main",
        "files": {"total": sum(counts.values()), **counts},
        "bytes": {
            "total": total,
            **sizes,
            "banked": banked,
            "remaining_network": max(0, total - banked - sizes["moved"]),
        },
        "scratch_bytes": 0,
    }


def test_download_lines_fresh_shows_total_and_empty_bar():
    lines = _download_lines(_est())
    assert lines[0].startswith("  download:     ░")
    assert "0 B / 8.0 GiB" in lines[0]
    assert "  0.0%" in lines[0]
    assert lines[1] == (
        "                nothing banked yet — full 8.0 GiB in 4 files to fetch"
    )


def test_download_lines_zero_total_skips_bar():
    lines = _download_lines(_est(total=0, count=0))
    assert lines == ["  download:     nothing to fetch (no sized files upstream)"]


def test_download_lines_banked_breakdown_adds_up():
    gib = 1024**3
    t = _transfer(
        total=8 * gib,
        verified=(2, 3 * gib),
        partial=(1, gib),
        missing=(3, 4 * gib),
    )
    lines = _download_lines(_est(t))
    assert " 50.0%" in lines[0]
    assert "4.0 GiB / 8.0 GiB" in lines[0]
    assert (
        "banked 4.0 GiB = 3.0 GiB verified in 2 files + 1.0 GiB partial in 1 file"
        in lines[1]
    )
    # partial + missing files still owe network bytes
    assert "still to fetch 4.0 GiB in 4 files" in lines[2]
    assert len(lines) == 3


def test_download_lines_unverified_and_ledgerless_note():
    t = _transfer(
        unverified=(2, 4 * 1024**3), missing=(2, 4 * 1024**3), has_ledger=False
    )
    lines = _download_lines(_est(t))
    assert "4.0 GiB unverified in 2 files" in lines[1]
    assert any("no transfer ledger" in line for line in lines)


def test_download_lines_complete_but_unregistered():
    t = _transfer(verified=(4, 8 * 1024**3))
    lines = _download_lines(_est(t))
    assert "100.0%" in lines[0]
    assert "nothing left to fetch — next archive run verifies and registers" in lines[2]


def test_download_lines_registered_bundle():
    t = _transfer(verified=(4, 8 * 1024**3), status="registered")
    lines = _download_lines(_est(t))
    assert "100.0%" in lines[0]
    assert "bundle already archived — nothing left to fetch" in lines[1]
    assert len(lines) == 2


def test_download_lines_skeleton_counts_moved_and_says_assemble():
    gib = 1024**3
    # Half verified here, half moved to another vault, nothing left to fetch.
    t = _transfer(
        total=8 * gib,
        verified=(2, 4 * gib),
        moved=(2, 4 * gib),
    )
    lines = _download_lines(_est(t))
    # Moved bytes count toward the pin's progress: exists-anywhere == 100%.
    assert "100.0%" in lines[0]
    assert "8.0 GiB / 8.0 GiB" in lines[0]
    assert any("4.0 GiB in 2 files moved to another vault" in line for line in lines)
    assert any(
        "assemble with the vault holding the moved files" in line for line in lines
    )


def test_download_lines_skeleton_with_a_half_still_to_fetch():
    gib = 1024**3
    t = _transfer(
        total=8 * gib,
        moved=(2, 4 * gib),
        missing=(2, 4 * gib),
    )
    lines = _download_lines(_est(t))
    assert " 50.0%" in lines[0]  # moved half done, other half owed
    assert any("still to fetch 4.0 GiB in 2 files" in line for line in lines)


def test_download_lines_notes_moved_upstream_revision():
    t = _transfer(verified=(1, 2 * 1024**3), missing=(3, 6 * 1024**3), pinned="f" * 40)
    lines = _download_lines(_est(t, revision="e" * 40))
    assert any(
        f"resumes pinned revision {'f' * 12}" in line and ("e" * 12) in line
        for line in lines
    )


def test_download_lines_color_uses_panel_styling():
    lines = _download_lines(_est(), color=True)
    assert "\033[2m" in lines[0]  # dim empty bar
    assert "\033[1m" in lines[0]  # bold percent / total
    plain = _download_lines(_est(), color=False)
    assert "\033[" not in plain[0]


def test_dominant_format_by_bytes_then_count():
    from darsay.estimate import _dominant_format

    assert _dominant_format([]) is None
    assert (
        _dominant_format(
            [
                {"path": "a.safetensors", "size": 10},
                {"path": "b-Q4_K_M.gguf", "size": 100},
                {"path": "c-Q8_0.gguf", "size": 100},
            ]
        )
        == "gguf"
    )
    # All sizes unknown: count decides.
    assert (
        _dominant_format(
            [
                {"path": "a.bin", "size": None},
                {"path": "b.gguf", "size": None},
                {"path": "c.gguf", "size": None},
            ]
        )
        == "gguf"
    )
    assert _dominant_format([{"path": "weights", "size": 1}]) == "(none)"
