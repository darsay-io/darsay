"""A code bundle end to end through the fake provider: pin, transfer,
register, the record, the derived views, and every verb that must either
work on it or refuse it by name."""

from __future__ import annotations

import hashlib
import json

import pytest

from darsay.archiver import load_manifest
from darsay.cli import main
from darsay.estimate import estimate, print_estimate
from darsay.export import export_bundle, import_bundle
from darsay.readme_gen import write_bundle_readme
from darsay.smoke import run_smoke
from darsay.verify import verify_bundle
from tests.conftest import silent
from tests.integration.conftest import archive_quiet
from tests.payloads import code_files

REPOSITORY = {
    "description": "Serve acme/toy on one box",
    "homepage": "https://example.invalid/recipe",
    "topics": ["vllm", "recipe"],
    "languages": {"Shell": 60, "Python": 40},
    "default_branch": "main",
    "archived_upstream": False,
    "fork": False,
    "parent": None,
    "forks_count": 2,
    "stars": 9,
    "submodules": None,
    "symlinks": None,
    "lfs_file_count": 0,
}


def _add_recipe(test_provider, **kwargs):
    return test_provider.add_repo(
        "acme/recipe",
        code_files(),
        pipeline_tag=None,
        license_id="mit",
        metadata={
            "card_data": {"license": "mit"},
            "tags": ["vllm", "recipe"],
            "gated": False,
            "created_at": "2026-09-04T11:23:46+00:00",
            "last_modified": "2026-09-05T12:26:24+00:00",
            "downloads": None,
            "likes": 9,
            "repository": dict(REPOSITORY),
        },
        **kwargs,
    )


def test_archive_registers_a_code_bundle_under_code(vault, test_provider):
    files = code_files()
    _add_recipe(test_provider)
    bundle = archive_quiet("test:code/acme/recipe", vault=vault)
    assert bundle.parent.name == "test--code--acme--recipe"
    m = load_manifest(bundle)
    assert m["artifact_type"] == "code"
    assert m["source"]["address"] == "test:code/acme/recipe"
    assert m["inventory"]["layout"]["payload_root"] == "code/"
    assert m["identity"]["publisher"] == "acme"
    assert m["identity"]["model_name"] == "recipe"
    for name, data in files.items():
        assert (bundle / "code" / name).read_bytes() == data
    assert not (bundle / "model").exists()
    # The per-type section, and only that section.
    assert "model_metadata" not in m and "runtime" not in m
    assert "dataset_metadata" not in m
    cm = m["code_metadata"]
    assert cm["description"] == "Serve acme/toy on one box"
    assert cm["languages"] == {"Shell": 60, "Python": 40}
    assert cm["default_branch"] == "main"
    assert cm["topics"] == ["vllm", "recipe"]
    found = cm["runtime_declarations"]["found"]
    assert cm["runtime_declarations"]["read_from"] == "inventory"
    assert found["container"] == ["Dockerfile"]
    assert found["compose"] == ["compose.yaml"]
    assert found["python"] == ["requirements.txt"]
    assert found["env_template"] == [".env.sample"]
    assert found["shell"] == ["start.sh", "stop.sh"]
    assert "node" not in found
    # Validation: checksums pass, completeness knows the recommended pair,
    # and no smoke test is pretended.
    val = m["validation"]
    assert val["checksum_verification"]["status"] == "pass"
    assert val["completeness"]["status"] == "complete"
    assert val["completeness"]["missing_recommended"] == []
    assert val["smoke_tests"] == {}
    assert m["licensing"]["spdx_id"] == "mit"
    assert (bundle / "LICENSE").read_bytes() == files["LICENSE"]
    assert (bundle / "SHA256SUMS").is_file()
    assert "_Why this code matters._" in (bundle / "curation.md").read_text()
    readme = (bundle / "README.md").read_text()
    assert "Archived code bundle" in readme
    assert (
        "| **Declares** | compose, container, env_template, python, shell |" in readme
    )
    assert "Serve acme/toy on one box" in readme
    assert "a code bundle has no engine" in readme
    assert "checkout " + m["source"]["revision"] in readme
    assert m["security"]["integrity_status"] == "verified-against-upstream"


def test_code_bundle_verifies_regens_exports_and_imports(
    vault, test_provider, tmp_path
):
    _add_recipe(test_provider)
    bundle = archive_quiet("test:code/acme/recipe", vault=vault)
    assert verify_bundle(bundle, progress=silent)["result"] == "pass"
    # regen is idempotent on a fresh bundle.
    assert write_bundle_readme(bundle, load_manifest(bundle)) == (0, 0)
    # A code bundle exports deterministically and lands verified elsewhere.
    tar1 = export_bundle(bundle, tmp_path / "e1", progress=silent)
    tar2 = export_bundle(bundle, tmp_path / "e2", progress=silent)
    assert (
        hashlib.sha256(tar1.read_bytes()).digest()
        == hashlib.sha256(tar2.read_bytes()).digest()
    )
    other = tmp_path / "other-vault"
    other.mkdir()
    landed = import_bundle(tar1, other, progress=silent)
    assert (landed / "code" / "start.sh").is_file()
    assert load_manifest(landed)["artifact_type"] == "code"
    assert verify_bundle(landed, progress=silent)["result"] == "pass"
    # Tampering with the tree is caught like any payload.
    (bundle / "code" / "start.sh").write_bytes(b"#!/bin/sh\nrm -rf /\n")
    assert verify_bundle(bundle, progress=silent)["result"] == "fail"


def test_estimate_prices_a_code_repository(vault, test_provider):
    _add_recipe(test_provider)
    est = estimate("test:code/acme/recipe", vault=vault, progress=silent)
    assert est["artifact_type"] == "code"
    assert est["payload"]["files"]["count"] == len(code_files())
    assert est["payload"]["support"] == {"count": 0, "bytes": 0}
    assert est["payload"]["total_size_bytes"] == sum(
        len(d) for d in code_files().values()
    )
    assert est["engines"] == []
    assert est["completeness"]["status"] == "complete"
    assert est["parameters"] is None and est["precision"] is None
    assert est["classification"] is None
    assert est["size_basis"] == "repository"
    assert est["formats"]["sh"]["file_count"] == 2
    assert est["estimates"]["min_ram_gb"] is None
    assert "code bundles" in est["estimates"]["notes"]
    code = est["code"]
    assert code["description"] == "Serve acme/toy on one box"
    assert sorted(code["runtime_declarations"]["found"]) == [
        "compose",
        "container",
        "env_template",
        "python",
        "shell",
    ]
    lines = []
    print_estimate(est, progress=lines.append)
    text = "\n".join(lines)
    assert "about:        Serve acme/toy on one box  [upstream]" in text
    assert "languages:    Shell 60%, Python 40%  [upstream]" in text
    assert (
        "declares:     compose (1), container (1), env_template (1), python (1), shell (2)"
        in text
    )
    assert "engines:      none (code bundle — hydrate/run not applicable)" in text
    assert "family:       recipe  [read from the name]" in text
    assert "parameters:" not in text and "precision:" not in text
    assert "To archive: darsay archive test:code/acme/recipe" in text
    # The catalog hint vocabulary copes with a payload that has no weights.
    from darsay.catalog import estimate_digest, hints_for

    assert hints_for(est) == []
    assert estimate_digest(est)["artifact_type"] == "code"


def test_estimate_json_and_hints_via_the_cli(vault, test_provider, capsys):
    _add_recipe(test_provider)
    assert (
        main(["--vault", str(vault), "estimate", "test:code/acme/recipe", "--json"])
        == 0
    )
    est = json.loads(capsys.readouterr().out)
    assert est["artifact_type"] == "code"
    assert est["code"]["runtime_declarations"]["counts"]["shell"] == 2


def test_hydrate_and_run_refuse_a_code_bundle_by_name(vault, test_provider):
    _add_recipe(test_provider)
    bundle = archive_quiet("test:code/acme/recipe", vault=vault)
    bundle_id = f"{bundle.parent.name}@{bundle.name}"
    for verb in ("hydrate", "run"):
        with pytest.raises(SystemExit) as exc:
            main(["--vault", str(vault), verb, bundle_id])
        assert "code bundles have no inference engine" in str(exc.value)
        assert "open code/ directly" in str(exc.value)


def test_smoke_records_nothing_for_a_code_bundle(vault, test_provider):
    _add_recipe(test_provider)
    bundle = archive_quiet("test:code/acme/recipe", vault=vault)
    notes = []
    assert run_smoke(bundle, progress=notes.append) == {}
    assert any("No smoke test applies to a code bundle" in n for n in notes)
    assert load_manifest(bundle)["validation"]["smoke_tests"] == {}


def test_info_and_list_show_the_code_bundle(vault, test_provider, capsys):
    _add_recipe(test_provider)
    bundle = archive_quiet("test:code/acme/recipe", vault=vault)
    bundle_id = f"{bundle.parent.name}@{bundle.name}"
    assert main(["--vault", str(vault), "info", bundle_id]) == 0
    out = capsys.readouterr().out
    assert "(schema v" in out and ", code)" in out
    assert "about:      Serve acme/toy on one box" in out
    assert "languages:  Shell, Python" in out
    assert "declares:   compose, container, env_template, python, shell" in out
    assert "hydration:" not in out
    assert main(["--vault", str(vault), "list", "--json"]) == 0
    rows = json.loads(capsys.readouterr().out)
    row = next(r for r in rows if r["bundle_id"] == bundle_id)
    assert row["artifact_type"] == "code"


def test_archive_twice_points_at_info_not_run(vault, test_provider):
    _add_recipe(test_provider)
    archive_quiet("test:code/acme/recipe", vault=vault)
    with pytest.raises(SystemExit, match="already exists") as exc:
        archive_quiet("test:code/acme/recipe", vault=vault)
    assert " info " in str(exc.value)
    assert " run " not in str(exc.value)
