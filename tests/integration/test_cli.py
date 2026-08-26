from __future__ import annotations

import json

import pytest

from darsay.cli import main
from tests.integration.conftest import archive_quiet
from tests.payloads import model_files


def test_cli_estimate_json_and_human(vault, test_provider, capsys):
    test_provider.add_repo("acme/toy", model_files())
    assert main(["--vault", str(vault), "estimate", "test:acme/toy", "--json"]) == 0
    out = capsys.readouterr().out
    data = json.loads(out[out.index("{") :])
    assert data["source"]["address"] == "test:acme/toy"
    assert data["payload"]["file_count"] == len(model_files())

    assert main(["--vault", str(vault), "estimate", "test:acme/toy", "--variants"]) == 0
    human = capsys.readouterr().out
    assert "test:acme/toy" in human
    assert "payload:" in human
    assert "To archive:" in human


def test_cli_list_partial_bundle(vault, test_provider, capsys):
    test_provider.add_repo("acme/toy", model_files())
    assert (
        main(
            [
                "--vault",
                str(vault),
                "archive",
                "test:acme/toy",
                "--max-bytes",
                "1",
                "--jobs",
                "1",
            ]
        )
        == 10
    )
    capsys.readouterr()
    assert main(["--vault", str(vault), "list"]) == 0
    listed = capsys.readouterr().out
    assert "partial" in listed
    assert "test:acme/toy" in listed


def test_run_joins_unquoted_prompt_and_accepts_repl_flag(vault, test_provider, monkeypatch):
    test_provider.add_repo("acme/toy", model_files())
    bundle = archive_quiet("test:acme/toy", vault=vault)
    seen = {}

    def fake_run(bundle_dir, prompt=None, **kwargs):
        seen["prompt"] = prompt
        seen["repl"] = kwargs.get("repl")
        seen["bundle"] = bundle_dir
        return {"status": "pass"}

    monkeypatch.setattr("darsay.hydrate.run_bundle", fake_run)
    assert main(["--vault", str(vault), "run", "acme--toy", "Say", "hello"]) == 0
    assert seen["prompt"] == "Say hello"
    assert seen["repl"] is False
    assert seen["bundle"] == bundle
    assert main(["--vault", str(vault), "run", "acme--toy", "--repl"]) == 0
    assert seen["repl"] is True
    assert seen["prompt"] is None


def test_list_json_and_ids(vault, test_provider, capsys):
    test_provider.add_repo("acme/toy", model_files())
    bundle = archive_quiet("test:acme/toy", vault=vault)
    assert main(["--vault", str(vault), "list", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data[0]["bundle_id"].startswith("test--acme--toy")
    assert data[0]["path"] == str(bundle)
    assert data[0]["partial"] is False
    assert "on_disk_bytes" in data[0]
    assert data[0]["payload_bytes"] > 0
    assert main(["--vault", str(vault), "list", "--ids"]) == 0
    assert "test--acme--toy@" in capsys.readouterr().out


def test_rm_yes_deletes_bundle(vault, test_provider, capsys):
    test_provider.add_repo("acme/toy", model_files())
    bundle = archive_quiet("test:acme/toy", vault=vault)
    assert bundle.is_dir()
    assert main(["--vault", str(vault), "rm", "--yes", "acme--toy"]) == 0
    assert not bundle.exists()
    assert "Removed" in capsys.readouterr().out
    assert main(["--vault", str(vault), "list"]) == 0
    assert "No bundles" in capsys.readouterr().out


def test_du_counts_bundle_bytes(vault, test_provider, capsys):
    test_provider.add_repo("acme/toy", model_files())
    archive_quiet("test:acme/toy", vault=vault)
    assert main(["--vault", str(vault), "du", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["bundles_bytes"] > 0
    assert data["total_bytes"] >= data["bundles_bytes"]
    assert data["bundles"][0]["bundle_id"].startswith("test--acme--toy")
    assert data["bundles"][0]["on_disk_bytes"] > 0


def test_complete_scripts(capsys):
    assert main(["complete", "zsh"]) == 0
    zsh = capsys.readouterr().out
    assert "#compdef darsay" in zsh
    assert "list --ids" in zsh
    assert main(["complete", "bash"]) == 0
    assert "complete -F _darsay darsay" in capsys.readouterr().out
    with pytest.raises(SystemExit):
        main(["complete", "tcsh"])


def test_list_empty_vault(vault, capsys):
    assert main(["--vault", str(vault), "list"]) == 0
    assert "No bundles" in capsys.readouterr().out


def test_vault_flag_after_subcommand(vault, test_provider, capsys):
    test_provider.add_repo("acme/toy", model_files())
    archive_quiet("test:acme/toy", vault=vault)
    assert main(["list", "--vault", str(vault)]) == 0
    listed = capsys.readouterr().out
    assert "test--acme--toy" in listed


def test_implicit_vault_announces_on_stderr(monkeypatch, tmp_path, capsys):
    monkeypatch.delenv("DARSAY_HOME", raising=False)
    monkeypatch.setattr("darsay.vault.default_vault", lambda: tmp_path / "darsay")
    assert main(["list"]) == 0
    captured = capsys.readouterr()
    assert "No bundles" in captured.out
    assert "default; set $DARSAY_HOME or --vault" in captured.err


def test_info_ambiguous_prefix_errors(vault, test_provider, capsys):
    test_provider.add_repo("acme/toy", model_files())
    test_provider.add_repo("acme/other", model_files())
    archive_quiet("test:acme/toy", vault=vault)
    archive_quiet("test:acme/other", vault=vault)
    with pytest.raises(SystemExit, match="matches 2 bundles"):
        main(["--vault", str(vault), "info", "acme"])


def test_list_info_verify_regen_roundtrip(vault, test_provider, capsys):
    test_provider.add_repo("acme/toy", model_files())
    bundle = archive_quiet("test:acme/toy", vault=vault)

    assert main(["--vault", str(vault), "list"]) == 0
    listed = capsys.readouterr().out
    assert "test--acme--toy" in listed
    assert "test:acme/toy" in listed
    assert "have" in listed
    assert main(["--vault", str(vault), "list", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data[0]["path"] == str(bundle)
    assert data[0]["license"] == "apache-2.0"

    assert main(["--vault", str(vault), "info", "test--acme--toy"]) == 0
    by_id = capsys.readouterr().out
    assert "test:acme/toy" in by_id

    before_info = (bundle / "manifest.json").read_bytes()
    assert main(["--vault", str(vault), "info", str(bundle)]) == 0
    info = capsys.readouterr().out
    assert "test:acme/toy" in info
    assert "schema v" in info
    assert (bundle / "manifest.json").read_bytes() == before_info

    assert main(["--vault", str(vault), "verify", str(bundle)]) == 0

    curation = bundle / "curation.md"
    text = curation.read_text()
    curation.write_text(text.replace("_Why this model matters._", "It was a fixture."))
    readme_before = (bundle / "README.md").read_text()
    assert main(["--vault", str(vault), "regen", str(bundle)]) == 0
    readme_after = (bundle / "README.md").read_text()
    assert "It was a fixture." in readme_after
    assert readme_before != readme_after


def test_cli_export_import(vault, test_provider, tmp_path):
    test_provider.add_repo("acme/toy", model_files())
    src_vault = tmp_path / "src"
    src_vault.mkdir()
    bundle = archive_quiet("test:acme/toy", vault=src_vault)
    out = tmp_path / "exports"
    out.mkdir()
    assert main(["--vault", str(src_vault), "export", str(bundle), "-o", str(out)]) == 0
    tars = list(out.glob("*.mvb.tar"))
    assert len(tars) == 1
    assert main(["--vault", str(vault), "import", str(tars[0])]) == 0
    assert list(vault.glob("*/*/manifest.json"))


def test_cli_archive_budget_exit_10(vault, test_provider):
    test_provider.add_repo("acme/toy", model_files())
    rc = main(
        [
            "--vault",
            str(vault),
            "archive",
            "test:acme/toy",
            "--max-bytes",
            "1",
            "--jobs",
            "1",
        ]
    )
    assert rc == 10


def test_smoke_dataset_structure_via_cli(vault, test_provider, capsys):
    from tests.payloads import dataset_files

    test_provider.add_repo(
        "acme/reviews",
        dataset_files(),
        pipeline_tag=None,
        license_id="mit",
        metadata={"card_data": {"license": "mit"}, "tags": [], "gated": False},
    )
    bundle = archive_quiet("test:datasets/acme/reviews", vault=vault)
    rc = main(["--vault", str(vault), "smoke", str(bundle)])
    assert rc == 0
    out = capsys.readouterr().out
    results = json.loads(out[out.index("{") :])
    assert results["structure"]["status"] == "pass"
