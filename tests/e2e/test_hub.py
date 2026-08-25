"""Live Hugging Face Hub path. Opt-in: ``pytest --run-e2e`` or ``DARSAY_E2E=1``.

Uses ``sshleifer/tiny-gpt2`` — the project's documented scratch repo — so the
run is small (a few megabytes) and still exercises estimate → archive →
verify → export → import against a real provider.
"""

from __future__ import annotations

from darsay.archiver import load_manifest
from darsay.cli import main
from darsay.export import export_bundle, import_bundle
from darsay.hashing import hash_file
from darsay.verify import verify_bundle
from tests.conftest import silent

TINY = "sshleifer/tiny-gpt2"


def test_tiny_gpt2_estimate_archive_verify_export_import(tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    assert main(["--vault", str(vault), "estimate", TINY]) in (0, 1)

    rc = main(["--vault", str(vault), "archive", TINY, "--jobs", "2"])
    assert rc == 0
    bundles = list(vault.glob("sshleifer--tiny-gpt2/*/manifest.json"))
    assert len(bundles) == 1
    bundle = bundles[0].parent
    manifest = load_manifest(bundle)
    assert manifest["source"]["provider"] == "huggingface"
    assert manifest["source"]["repo_id"] == TINY
    assert manifest["artifact_type"] == "model"
    assert (bundle / "model").is_dir()

    report = verify_bundle(bundle, progress=silent)
    assert report["checksum"]["status"] == "pass"

    tar_path = export_bundle(bundle, tmp_path / "exports", progress=silent)
    other = tmp_path / "other-vault"
    imported = import_bundle(tar_path, other, progress=silent)
    assert (
        load_manifest(imported)["inventory"]["bundle_hash"]["value"]
        == manifest["inventory"]["bundle_hash"]["value"]
    )
    assert hash_file(tar_path, with_blake3=False)
