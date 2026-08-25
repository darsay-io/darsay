from __future__ import annotations

from modelvault.estimate import _format_breakdown


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
