from __future__ import annotations

import hashlib

from darsay.hashing import bundle_hash, hash_file, iter_payload_files


def test_hash_file_sha256(tmp_path):
    path = tmp_path / "blob.bin"
    path.write_bytes(b"hello vault")
    got = hash_file(path, with_blake3=False)
    assert got["sha256"] == hashlib.sha256(b"hello vault").hexdigest()
    assert "git_sha1" not in got


def test_hash_file_git_sha1(tmp_path):
    data = b"hello vault"
    path = tmp_path / "blob.bin"
    path.write_bytes(data)
    expected = hashlib.sha1()
    expected.update(b"blob %d\0" % len(data))
    expected.update(data)
    got = hash_file(path, with_blake3=False, with_git_sha1=True)
    assert got["git_sha1"] == expected.hexdigest()


def test_hash_file_blake3_optional(tmp_path):
    path = tmp_path / "blob.bin"
    path.write_bytes(b"x")
    from darsay.hashing import HAVE_BLAKE3

    got = hash_file(path, with_blake3=True)
    if HAVE_BLAKE3:
        assert "blake3" in got and len(got["blake3"]) == 64
    else:
        assert "blake3" not in got


def test_bundle_hash_is_sorted_and_deterministic():
    a = bundle_hash(
        [{"path": "model/b.bin", "sha256": "bb"}, {"path": "model/a.bin", "sha256": "aa"}]
    )
    b = bundle_hash(
        [{"path": "model/a.bin", "sha256": "aa"}, {"path": "model/b.bin", "sha256": "bb"}]
    )
    assert a["value"] == b["value"]
    assert a["algorithm"] == "sha256-of-sorted-sha256-lines"
    assert a["covers"].startswith("model/")
    expected_lines = "aa  model/a.bin\nbb  model/b.bin\n"
    assert a["value"] == hashlib.sha256(expected_lines.encode("utf-8")).hexdigest()


def test_bundle_hash_covers_payload_root_name():
    record = bundle_hash([{"path": "data/x", "sha256": "aa"}], "data")
    assert "data/" in record["covers"]


def test_iter_payload_files_skips_cache_and_sorts(tmp_path):
    payload = tmp_path / "model"
    (payload / "z.txt").parent.mkdir()
    (payload / "z.txt").write_text("z")
    (payload / "a.txt").write_text("a")
    nested = payload / ".cache" / "huggingface" / "partial"
    nested.mkdir(parents=True)
    (nested / "incomplete").write_bytes(b"not payload")
    names = [rel for rel, _path in iter_payload_files(payload)]
    assert names == ["a.txt", "z.txt"]
