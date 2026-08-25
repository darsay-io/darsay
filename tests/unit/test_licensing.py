from __future__ import annotations

from darsay.licensing import LICENSE_INFO, build_licensing_record, find_license_files


def test_known_spdx_flags(tmp_path):
    (tmp_path / "LICENSE").write_text("Apache License 2.0\n")
    record = build_licensing_record("apache-2.0", tmp_path)
    info = LICENSE_INFO["apache-2.0"]
    assert record["name"] == info["name"]
    assert record["commercial_use"] is True
    assert record["patent_grant"] is True
    assert record["redistribution"] is True
    assert record["modification"] is True
    assert record["needs_manual_review"] is False
    assert record["notes"] is None


def test_unknown_spdx_does_not_fabricate_flags(tmp_path):
    record = build_licensing_record("proprietary-xyz", tmp_path)
    assert record["spdx_id"] == "proprietary-xyz"
    assert record["name"] is None
    assert record["commercial_use"] is None
    assert record["redistribution"] is None
    assert record["needs_manual_review"] is True
    assert "not in the rights-flags table" in (record["notes"] or "")


def test_missing_license_file_noted(tmp_path):
    record = build_licensing_record("mit", tmp_path)
    assert "no license text file" in (record["notes"] or "")


def test_gated_forces_manual_review(tmp_path):
    (tmp_path / "LICENSE").write_text("MIT\n")
    record = build_licensing_record("mit", tmp_path, gated=True)
    assert record["needs_manual_review"] is True
    assert "gated" in (record["notes"] or "").lower()
    assert record["license_files"] == [f"{tmp_path.name}/LICENSE"]


def test_find_license_files_sorted(tmp_path):
    (tmp_path / "NOTICE").write_text("n")
    (tmp_path / "LICENSE").write_text("l")
    assert find_license_files(tmp_path) == ["LICENSE", "NOTICE"]


def test_null_spdx(tmp_path):
    record = build_licensing_record(None, tmp_path)
    assert record["spdx_id"] is None
    assert record["needs_manual_review"] is True
