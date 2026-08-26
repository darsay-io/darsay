from __future__ import annotations

from darsay.smoke import (
    _check_csv,
    _check_jsonl,
    _check_parquet,
    structure_test,
    tokenizer_test,
)
from tests.payloads import parquet_magic_file


def test_parquet_magic(tmp_path):
    good = tmp_path / "ok.parquet"
    good.write_bytes(parquet_magic_file())
    assert _check_parquet(good) is None
    bad = tmp_path / "bad.parquet"
    bad.write_bytes(b"XXXX" + b"payload" + b"XXXX")
    assert "PAR1" in (_check_parquet(bad) or "")
    tiny = tmp_path / "tiny.parquet"
    tiny.write_bytes(b"PAR1")
    assert "too small" in (_check_parquet(tiny) or "")


def test_jsonl_first_line(tmp_path):
    good = tmp_path / "rows.jsonl"
    good.write_text('{"a": 1}\n{"a": 2}\n')
    assert _check_jsonl(good) is None
    empty = tmp_path / "empty.jsonl"
    empty.write_text("\n")
    assert "empty" in (_check_jsonl(empty) or "")
    junk = tmp_path / "junk.jsonl"
    junk.write_text("not json\n")
    assert "not valid JSON" in (_check_jsonl(junk) or "")


def test_csv_sniff(tmp_path):
    good = tmp_path / "t.csv"
    good.write_text("a,b\n1,2\n")
    assert _check_csv(good) is None
    blank = tmp_path / "blank.csv"
    blank.write_text("   ")
    assert "empty" in (_check_csv(blank) or "")


def test_structure_test_pass_and_skip(tmp_path):
    payload = tmp_path / "data"
    payload.mkdir()
    (payload / "train.jsonl").write_text('{"x": 1}\n')
    (payload / "notes.txt").write_text("ignore")
    result = structure_test(payload)
    assert result["status"] == "pass"
    assert result["files_checked"] == 1
    empty = tmp_path / "empty"
    empty.mkdir()
    skipped = structure_test(empty)
    assert skipped["status"] == "skipped"


def test_tokenizer_skipped_without_engines(tmp_path, monkeypatch):
    # Force both optional imports to fail so we assert the skip record, not a crash.
    import builtins

    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name.split(".")[0] in {"tokenizers", "transformers"}:
            raise ImportError(name)
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    result = tokenizer_test(tmp_path)
    assert result["status"] == "skipped"
    assert "neither" in result["reason"] or "tokenizers" in result["reason"]
