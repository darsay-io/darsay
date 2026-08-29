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
    assert "download:" in human
    assert "nothing banked yet" in human
    assert "To archive:" in human


def test_cli_estimate_reports_banked_transfer_state(vault, test_provider, capsys):
    files = model_files()
    test_provider.add_repo("acme/toy", files)
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

    # Bank a resumable partial for one still-missing file, as an interrupted
    # provider download would.
    bundle_dir = next((vault / "test--acme--toy").iterdir())
    weight = "model.safetensors"
    incomplete = bundle_dir / "model" / ".cache" / "test" / f"{weight}.incomplete"
    incomplete.parent.mkdir(parents=True, exist_ok=True)
    incomplete.write_bytes(files[weight][: len(files[weight]) // 2])

    assert main(["--vault", str(vault), "estimate", "test:acme/toy", "--json"]) == 0
    out = capsys.readouterr().out
    transfer = json.loads(out[out.index("{") :])["transfer"]
    assert transfer["status"] == "in_progress"
    assert transfer["has_ledger"] is True
    sizes = transfer["bytes"]
    assert sizes["verified"] > 0
    assert sizes["partial"] == len(files[weight]) // 2
    assert sizes["banked"] == sizes["verified"] + sizes["unverified"] + sizes["partial"]
    assert sizes["banked"] + sizes["remaining_network"] == sizes["total"]
    assert transfer["pinned_revision"] == "a" * 40

    assert main(["--vault", str(vault), "estimate", "test:acme/toy"]) == 0
    human = capsys.readouterr().out
    assert "banked" in human
    assert "partial" in human
    assert "still to fetch" in human
    assert "in progress — archive resumes here" in human
    assert "more," in human  # disk line prices only the remaining bytes


def test_cli_estimate_adopts_ledgerless_payload(vault, test_provider, capsys):
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
    bundle_dir = next((vault / "test--acme--toy").iterdir())
    (bundle_dir / "transfer.json").unlink()
    capsys.readouterr()

    assert main(["--vault", str(vault), "estimate", "test:acme/toy", "--json"]) == 0
    out = capsys.readouterr().out
    data = json.loads(out[out.index("{") :])
    transfer = data["transfer"]
    assert transfer["has_ledger"] is False
    assert transfer["bytes"]["verified"] == 0
    assert transfer["bytes"]["unverified"] > 0
    assert data["bundle"]["state"] == "adoptable"

    assert main(["--vault", str(vault), "estimate", "test:acme/toy"]) == 0
    human = capsys.readouterr().out
    assert "unverified" in human
    assert "no transfer ledger" in human


def test_cli_estimate_registered_bundle_has_nothing_to_fetch(
    vault, test_provider, capsys
):
    test_provider.add_repo("acme/toy", model_files())
    archive_quiet("test:acme/toy", vault=vault)
    assert main(["--vault", str(vault), "estimate", "test:acme/toy", "--json"]) == 0
    out = capsys.readouterr().out
    data = json.loads(out[out.index("{") :])
    assert data["transfer"]["status"] == "registered"
    assert data["transfer"]["bytes"]["remaining_network"] == 0
    assert data["disk"]["needed_bytes"] == 0
    assert data["bundle"]["state"] == "registered"

    assert main(["--vault", str(vault), "estimate", "test:acme/toy"]) == 0
    human = capsys.readouterr().out
    assert "100.0%" in human
    assert "bundle already archived — nothing left to fetch" in human


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
    assert "DESIRE" not in listed
    assert "NOTE" not in listed
    assert "remaining" in listed


def test_run_joins_unquoted_prompt_and_accepts_repl_flag(
    vault, test_provider, monkeypatch
):
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
    assert data[0]["status"] == "have"
    assert data[0]["source_address"] == "test:acme/toy"
    assert data[0]["remaining_bytes"] == 0
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
    assert "DESIRE" not in listed
    assert "NOTE" not in listed
    assert "remaining" not in listed
    assert main(["--vault", str(vault), "list", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data[0]["path"] == str(bundle)
    assert data[0]["license"] == "apache-2.0"

    assert main(["--vault", str(vault), "info", "test--acme--toy"]) == 0
    by_id = capsys.readouterr().out
    assert "test:acme/toy" in by_id
    assert str(bundle) in by_id
    assert "path:" in by_id

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


def test_archive_next_hint_is_run_for_models_info_for_datasets(
    vault, test_provider, capsys
):
    from tests.payloads import dataset_files

    test_provider.add_repo("acme/toy", model_files())
    assert main(["--vault", str(vault), "archive", "test:acme/toy"]) == 0
    model_out = capsys.readouterr().out
    assert "next:" in model_out
    assert "darsay run test--acme--toy@" in model_out

    test_provider.add_repo(
        "acme/reviews",
        dataset_files(),
        pipeline_tag=None,
        license_id="mit",
        metadata={"card_data": {"license": "mit"}, "tags": [], "gated": False},
    )
    assert main(["--vault", str(vault), "archive", "test:datasets/acme/reviews"]) == 0
    data_out = capsys.readouterr().out
    assert "darsay info " in data_out
    assert "darsay run " not in data_out.split("next:")[-1]


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


def test_catalog_index_ids_and_add_estimate(vault, test_provider, capsys):
    test_provider.add_repo("acme/toy", model_files())
    assert main(["--vault", str(vault), "catalog"]) == 0
    empty = capsys.readouterr().out
    assert "No catalogs" in empty
    assert (
        main(
            [
                "--vault",
                str(vault),
                "catalog",
                "new",
                "summer",
                "--title",
                "Summer 2026",
            ]
        )
        == 0
    )
    capsys.readouterr()
    assert main(["--vault", str(vault), "catalog"]) == 0
    listed = capsys.readouterr().out
    assert "summer" in listed
    assert "Summer 2026" in listed
    assert main(["--vault", str(vault), "catalog", "--ids"]) == 0
    assert capsys.readouterr().out.strip() == "summer"
    assert (
        main(
            [
                "--vault",
                str(vault),
                "catalog",
                "add",
                "summer",
                "test:acme/toy",
                "--desire",
                "8",
                "--estimate",
            ]
        )
        == 0
    )
    added = capsys.readouterr().out
    assert "Added test:acme/toy" in added
    catalog = json.loads((vault / "catalogs" / "summer" / "catalog.json").read_text())
    assert catalog["entries"][0]["estimate"]["payload_bytes"] > 0
    assert "checked_path" not in catalog["entries"][0]["estimate"]


def test_hydrate_dry_run_dehydrate_and_envs_via_cli(
    vault, test_provider, tmp_path, monkeypatch, capsys
):
    test_provider.add_repo("acme/toy", model_files())
    bundle = archive_quiet("test:acme/toy", vault=vault)
    runtime = tmp_path / "runtime"
    monkeypatch.setenv("DARSAY_RUNTIME", str(runtime))
    capsys.readouterr()
    assert main(["--vault", str(vault), "hydrate", "acme--toy", "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "engine:" in out or "transformers" in out
    assert not runtime.exists()
    assert not (bundle / "hydration.json").exists()

    (bundle / "hydration.json").write_text(
        json.dumps(
            {
                "hydration_schema": 1,
                "bundle_id": "x",
                "engine": "transformers",
                "env": {"key": "fake"},
                "runs": [],
            }
        )
        + "\n"
    )
    assert main(["--vault", str(vault), "dehydrate", "acme--toy"]) == 0
    assert not (bundle / "hydration.json").exists()
    assert main(["--vault", str(vault), "envs"]) == 0
    assert "No runtime envs" in capsys.readouterr().out


def test_assemble_via_cli(vault, test_provider, tmp_path, capsys):
    from darsay.transfer import PartialTransfer

    files = model_files()
    test_provider.add_repo("acme/toy", files)
    partials = []
    for shard in ((1, 2), (2, 2)):
        sub = tmp_path / f"lane{shard[0]}"
        sub.mkdir()
        try:
            archive_quiet("test:acme/toy", vault=sub, shard=shard, max_bytes=1)
        except PartialTransfer as stop:
            partials.append(str(stop.bundle_dir))
        else:
            found = list(sub.glob("*/*"))
            assert found
            partials.append(str(found[0]))
    capsys.readouterr()
    assert main(["--vault", str(vault), "assemble", *partials]) == 0
    out = capsys.readouterr().out
    assert "Combined partial bundle:" in out
    assert "archive" in out
    assert not list(vault.glob("*/*/manifest.json"))


def test_cli_run_records_via_stubbed_runner(vault, test_provider, monkeypatch, capsys):
    import sys

    from darsay.archiver import load_manifest

    test_provider.add_repo("acme/toy", model_files())
    bundle = archive_quiet("test:acme/toy", vault=vault)
    (bundle / "hydration.json").write_text(
        json.dumps(
            {
                "hydration_schema": 1,
                "bundle_id": load_manifest(bundle)["bundle_id"],
                "engine": "transformers",
                "weights": None,
                "env": {
                    "key": "transformers-py3.14-deadbeef",
                    "path": "/tmp/fake-env",
                    "python": "3.14.0",
                    "python_executable": sys.executable,
                },
                "runs": [],
            }
        )
        + "\n"
    )

    def fake_invoke(env_record, engine, runner_args, timeout=None):
        return 0, {
            "status": "pass",
            "output": "ok",
            "new_tokens": 2,
            "device": "cpu",
            "load_seconds": 0.0,
            "generate_seconds": 0.0,
            "tokens_per_second": None,
            "versions": {},
        }

    monkeypatch.setattr("darsay.hydrate._invoke_runner", fake_invoke)
    capsys.readouterr()
    assert main(["--vault", str(vault), "run", "acme--toy", "Hello"]) == 0
    out = capsys.readouterr().out
    assert "Run PASSED" in out
    assert "2 tokens" in out
    hyd = json.loads((bundle / "hydration.json").read_text())
    assert hyd["runs"][-1]["status"] == "pass"
    assert hyd["runs"][-1]["prompt"] == "Hello"


def test_cli_config_shows_effective_floor(vault, capsys, monkeypatch):
    monkeypatch.delenv("DARSAY_MIN_FREE", raising=False)
    (vault / "config.toml").write_text(
        '[transfer]\nmin_free = "10G"\n', encoding="utf-8"
    )
    assert main(["--vault", str(vault), "config"]) == 0
    out = capsys.readouterr().out
    assert "transfer.min_free = 10.0 GiB" in out
    assert str(vault / "config.toml") in out

    assert main(["--vault", str(vault), "config", "--json"]) == 0
    out = capsys.readouterr().out
    data = json.loads(out[out.index("{") :])
    setting = data["settings"]["transfer.min_free"]
    assert setting["value"] == 10 * 1024**3
    assert setting["origin"] == str(vault / "config.toml")


def test_cli_archive_min_free_pauses_with_exit_10(vault, test_provider, capsys):
    test_provider.add_repo("acme/toy", model_files())
    code = main(
        ["--vault", str(vault), "archive", "test:acme/toy", "--min-free", "1024T"]
    )
    assert code == 10
    out = capsys.readouterr().out
    assert "paused cleanly (disk:" in out
    assert "Free disk space, then re-run" in out


def test_cli_config_lists_rate_and_offline_settings(vault, capsys, monkeypatch):
    (vault / "config.toml").write_text(
        '[transfer]\nmax_rate = "5M"\nmax_offline = "30m"\n', encoding="utf-8"
    )
    assert main(["--vault", str(vault), "config"]) == 0
    out = capsys.readouterr().out
    assert "transfer.max_rate = 5.0 MiB/s" in out
    assert "transfer.max_offline = 30 min" in out
    assert "archive --max-rate" in out
    assert "$DARSAY_MAX_OFFLINE" in out


def test_cli_archive_offline_exit_10_with_reconnect_hint(vault, test_provider, capsys):
    test_provider.add_repo("acme/toy", model_files())
    test_provider.fail_next("model.safetensors", ConnectionResetError("reset"))
    code = main(
        [
            "--vault",
            str(vault),
            "archive",
            "test:acme/toy",
            "--max-offline",
            "0",
            "--jobs",
            "1",
        ]
    )
    assert code == 10
    out = capsys.readouterr().out
    assert "paused cleanly (offline: network unreachable (connection reset))" in out
    assert "Once the network is back, re-run" in out


def test_cli_archive_rate_cap_prints_plan_line(vault, test_provider, capsys):
    test_provider.add_repo("acme/toy", model_files())
    code = main(
        [
            "--vault",
            str(vault),
            "archive",
            "test:acme/toy",
            "--max-rate",
            "50M",
            "--dry-run",
        ]
    )
    assert code == 0
    out = capsys.readouterr().out
    assert "rate:     capped at 50.0 MiB/s" in out
