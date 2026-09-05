from __future__ import annotations

import json

import pytest

from darsay.archiver import load_manifest
from darsay.cli import main
from darsay.collection import publication
from tests.integration.conftest import archive_quiet
from tests.payloads import make_gguf, model_files


def pack(test_provider):
    files = model_files(
        extra={
            "model-Q4_K_M.gguf": make_gguf({"general.file_type": 15}),
            "model-Q8_0.gguf": make_gguf({"general.file_type": 7}),
            "mmproj-F16.gguf": make_gguf({"general.file_type": 1}),
        }
    )
    test_provider.add_repo("acme/pack", files)
    return files


def test_cancel_precedes_archive_directory_and_payload_writes(vault, test_provider):
    pack(test_provider)
    before = list(vault.rglob("*"))

    def cancel(snapshot, _pinned):
        inventory = publication(snapshot)
        assert len(inventory["variants"]) == 2
        assert len(inventory["companions"]) == 1
        assert list(vault.rglob("*")) == before
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        archive_quiet("test:acme/pack", vault=vault, choose=cancel)
    assert list(vault.rglob("*")) == before
    assert test_provider.downloads == []


def test_chosen_collection_pins_one_include_union(vault, test_provider):
    pack(test_provider)

    def choose(snapshot, _pinned):
        inventory = publication(snapshot)
        assert snapshot.revision == "a" * 40
        return [p for v in inventory["variants"] for p in v["include"]]

    bundle = archive_quiet("test:acme/pack", vault=vault, choose=choose)
    subset = load_manifest(bundle)["source"]["subset"]
    assert subset["include"] == ["/model-Q4_K_M.gguf", "/model-Q8_0.gguf"]
    assert "policy" not in subset
    assert (bundle / "model/README.md").is_file()
    assert not (bundle / "model/mmproj-F16.gguf").exists()
    assert not (bundle / "model/model.safetensors").exists()


def test_the_whole_publication_is_the_repository_itself(vault, test_provider):
    from darsay.catalog import include_key

    pack(test_provider)
    assert include_key(["/*"]) == include_key(None)
    bundle = archive_quiet("test:acme/pack", vault=vault, choose=lambda *_: ["/*"])
    subset = load_manifest(bundle)["source"]["subset"]
    # No subset, or the default policy's own record — never a ``/*`` selector.
    assert subset is None or (subset.get("policy") and subset["include"] != ["/*"])
    for name in ("model.safetensors", "model-Q4_K_M.gguf", "model-Q8_0.gguf"):
        assert (bundle / "model" / name).is_file()

    # Typed by hand it is the same scope: an unqualified rerun resumes it
    # without a prompt or a refusal, and --full is not a different identity.
    other = vault.parent / "other-vault"
    archive_quiet("test:acme/pack", vault=other, include=["/*"], dry_run=True)
    ledger = json.loads(next(other.glob("*/*/transfer.json")).read_text())
    assert (ledger.get("subset") or {}).get("include") != ["/*"]
    archive_quiet(
        "test:acme/pack",
        vault=other,
        choose=lambda *_: pytest.fail("a resumed pin must not prompt"),
        resume_scope=True,
        dry_run=True,
    )


@pytest.mark.parametrize(
    "options", [{"full": True}, {"include": ["/*"]}, {"shard": (0, 2)}]
)
def test_explicit_scope_bypasses_chooser(vault, test_provider, options):
    pack(test_provider)

    def unexpected(*_):
        pytest.fail("explicit scope must not prompt")

    archive_quiet(
        "test:acme/pack", vault=vault, choose=unexpected, dry_run=True, **options
    )


def test_resume_does_not_prompt_or_change_selected_scope(vault, test_provider):
    pack(test_provider)
    archive_quiet(
        "test:acme/pack",
        vault=vault,
        choose=lambda *_: ["/model-Q4_K_M.gguf"],
        dry_run=True,
    )

    def unexpected(*_):
        pytest.fail("resume must not prompt")

    bundle = archive_quiet(
        "test:acme/pack", vault=vault, choose=unexpected, resume_scope=True
    )
    assert load_manifest(bundle)["source"]["subset"]["include"] == [
        "/model-Q4_K_M.gguf"
    ]


@pytest.mark.parametrize(
    "tty,term,flags,prompts",
    [
        (True, "xterm-256color", [], True),
        (False, "xterm-256color", [], False),
        (True, "dumb", [], False),
        (True, "xterm", ["--yes"], False),
        (True, "xterm", ["--full"], False),
        (True, "xterm", ["--include", "*Q4*"], False),
    ],
)
def test_cli_only_opens_picker_for_an_interactive_fresh_scope(
    vault, test_provider, monkeypatch, tty, term, flags, prompts
):
    pack(test_provider)
    from darsay import cli, collection_tui

    called = []
    monkeypatch.setattr(cli, "_on_a_terminal", lambda: tty)
    monkeypatch.setenv("TERM", term)
    monkeypatch.setattr(
        collection_tui,
        "choose_collection",
        lambda s, _p: called.append(s.revision) or ["/*"],
    )
    assert (
        main(["--vault", str(vault), "archive", "test:acme/pack", "--dry-run", *flags])
        == 0
    )
    assert bool(called) is prompts


@pytest.mark.parametrize("arguments", [["--next", "room"], ["room", "--next"]])
def test_both_catalog_spellings_bypass_picker_and_require_row_scope(
    vault, test_provider, monkeypatch, arguments
):
    pack(test_provider)
    from darsay import cli, collection_tui

    monkeypatch.setattr(cli, "_on_a_terminal", lambda: True)
    monkeypatch.setenv("TERM", "xterm-256color")
    monkeypatch.setattr(
        collection_tui,
        "choose_collection",
        lambda *_: pytest.fail("catalog jobs must not prompt"),
    )
    assert main(["--vault", str(vault), "catalog", "new", "room"]) == 0
    assert (
        main(["--vault", str(vault), "catalog", "add", "room", "test:acme/pack"]) == 0
    )
    # A whole-repository row must not report a narrower, unfinished collection.
    archive_quiet(
        "test:acme/pack", vault=vault, include=["/model-Q4_K_M.gguf"], dry_run=True
    )
    with pytest.raises(SystemExit, match="this pin is a subset"):
        main(["--vault", str(vault), "archive", *arguments, "--dry-run"])


def test_direct_cli_resume_preserves_collection_in_a_pipe(
    vault, test_provider, monkeypatch
):
    pack(test_provider)
    from darsay import cli

    monkeypatch.setattr(cli, "_on_a_terminal", lambda: False)
    archive_quiet(
        "test:acme/pack", vault=vault, include=["/model-Q4_K_M.gguf"], dry_run=True
    )
    assert main(["--vault", str(vault), "archive", "test:acme/pack"]) == 0
    manifest = next(vault.glob("*/*/manifest.json"))
    assert load_manifest(manifest.parent)["source"]["subset"]["include"] == [
        "/model-Q4_K_M.gguf"
    ]


def test_a_pin_created_while_choosing_cannot_replace_reviewed_scope(
    vault, test_provider
):
    pack(test_provider)

    def choose(*_):
        # Another worker pins this source while the collector is in the TUI.
        archive_quiet("test:acme/pack", vault=vault, full=True, dry_run=True)
        return ["/model-Q4_K_M.gguf"]

    with pytest.raises(SystemExit, match="requested collection differs"):
        archive_quiet("test:acme/pack", vault=vault, choose=choose)
    assert test_provider.downloads == []


def _partial_holding_one_variant(vault, test_provider):
    """Pin two variants and stop once the first has landed: verified bytes on disk."""
    from darsay.transfer import PartialTransfer

    test_provider.add_repo(
        "acme/pack",
        model_files(
            extra={
                "model-Q4_K_M.gguf": make_gguf({"general.file_type": 15})
                + b"x" * 30000,
                "model-Q8_0.gguf": make_gguf({"general.file_type": 7}) + b"y" * 30000,
                "mmproj-F16.gguf": make_gguf({"general.file_type": 1}),
            }
        ),
    )
    with pytest.raises(PartialTransfer):
        archive_quiet(
            "test:acme/pack",
            vault=vault,
            include=["/model-Q4_K_M.gguf", "/model-Q8_0.gguf"],
            max_bytes=30500,
        )
    bundle = next(vault.glob("*/*/transfer.json")).parent
    landed = [p.name for p in (bundle / "model").iterdir() if p.suffix == ".gguf"]
    assert len(landed) == 1
    other = (
        "/model-Q8_0.gguf" if landed == ["model-Q4_K_M.gguf"] else "/model-Q4_K_M.gguf"
    )
    return bundle, landed[0], other


def test_force_names_what_it_removes_and_asks_first(vault, test_provider):
    bundle, landed, other = _partial_holding_one_variant(vault, test_provider)
    notes = []

    with pytest.raises(SystemExit, match="declined"):
        archive_quiet(
            "test:acme/pack",
            vault=vault,
            force=True,
            include=[other],
            progress=notes.append,
            confirm=lambda _q, **_: False,
        )
    assert (bundle / "model" / landed).is_file()
    assert any("outside the new scope" in n for n in notes)
    assert any(landed in n for n in notes)
    # Declining leaves the old pin in place: both selectors, ready to resume.
    ledger = json.loads((bundle / "transfer.json").read_text())
    assert ledger["subset"]["include"] == ["/model-Q4_K_M.gguf", "/model-Q8_0.gguf"]

    notes.clear()
    archive_quiet(
        "test:acme/pack",
        vault=vault,
        force=True,
        dry_run=True,
        include=[other],
        progress=notes.append,
        confirm=lambda *_, **__: pytest.fail(
            "a dry run removes nothing, so asks nothing"
        ),
    )
    assert any("nothing removed" in n for n in notes)
    assert (bundle / "model" / landed).is_file()

    notes.clear()
    archive_quiet(
        "test:acme/pack",
        vault=vault,
        force=True,
        include=[other],
        progress=notes.append,
        confirm=lambda _q, **_: True,
    )
    assert not (bundle / "model" / landed).exists()
    listed = next(i for i, n in enumerate(notes) if "outside the new scope" in n)
    assert listed < notes.index("Transfer plan:")


def test_force_opens_the_picker_on_the_pin_it_replaces(vault, test_provider):
    bundle, landed, other = _partial_holding_one_variant(vault, test_provider)
    offered = []

    def choose(_snapshot, pinned):
        offered.append(pinned)
        return [other]

    archive_quiet(
        "test:acme/pack",
        vault=vault,
        force=True,
        choose=choose,
        confirm=lambda _q, **_: True,
    )
    assert len(offered) == 1
    assert offered[0]["include"] == ["/model-Q4_K_M.gguf", "/model-Q8_0.gguf"]
    assert offered[0]["verified"] == 7  # six sidecars and the variant that landed
    assert offered[0]["verified_bytes"] > 30000
    assert not (bundle / "model" / landed).exists()


def test_explicit_force_can_repin_a_partial_collection(vault, test_provider):
    pack(test_provider)
    archive_quiet(
        "test:acme/pack", vault=vault, include=["/model-Q4_K_M.gguf"], dry_run=True
    )
    bundle = archive_quiet("test:acme/pack", vault=vault, force=True, full=True)
    assert load_manifest(bundle)["source"]["subset"] is None
    assert (bundle / "model/model-Q8_0.gguf").is_file()
    assert (bundle / "model/mmproj-F16.gguf").is_file()
