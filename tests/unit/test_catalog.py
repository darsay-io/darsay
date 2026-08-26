from __future__ import annotations

import json
from pathlib import Path

import pytest

from darsay.catalog import (
    CATALOG_KIND,
    CATALOG_SCHEMA_VERSION,
    DIGEST_KEYS,
    entry_key,
    estimate_digest,
    estimate_is_stale,
    filter_want,
    fold_slug,
    format_archive_command,
    include_key,
    load_catalog,
    new_catalog,
    next_entry,
    next_idle_message,
    overlay,
    overlay_stats,
    print_catalog_index,
    print_catalog_table,
    project_stored_estimate,
    realize_from_overlay,
    resolve_catalog,
    revisions_match,
    save_catalog,
    sort_rows,
    try_parse_source,
    try_resolve_catalog,
    upsert_entry,
    vault_header_line,
    drop_entry,
    adopt_entries,
    warning_detail,
    write_catalog_readme,
)


def _entry(source, *, desire=None, revision=None, include=None, estimate=None, note=None):
    return {
        "source": source,
        "revision": revision,
        "include": include,
        "desire": desire,
        "note": note,
        "added": "2026-01-01T00:00:00+00:00",
        "estimate": estimate,
    }


def _catalog(entries, **kwargs):
    cat = {
        "catalog_schema_version": CATALOG_SCHEMA_VERSION,
        "kind": CATALOG_KIND,
        "id": "summer",
        "title": "Summer 2026",
        "curator": "Alex",
        "note": None,
        "created": "2026-01-01T00:00:00+00:00",
        "updated": "2026-01-01T00:00:00+00:00",
        "entries": entries,
    }
    cat.update(kwargs)
    return cat


def _record(source, *, revision="aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", partial=False, include=None, remaining=0):
    return {
        "bundle_id": f"test--acme--toy@{revision[:12]}",
        "path": "/tmp/bundle",
        "license": "apache-2.0",
        "on_disk_bytes": 100,
        "payload_bytes": 1000,
        "size": "1.0 KiB",
        "integrity": "verified-against-upstream" if not partial else "archiving: 23%",
        "archived": "2026-08-01",
        "partial": partial,
        "status": "partial" if partial else "have",
        "source_address": source,
        "revision": revision,
        "revision_ref": "main",
        "include": include,
        "remaining_bytes": remaining if partial else 0,
        "percent": 23 if partial else None,
        "artifact_type": "model",
    }


def test_revisions_match_table():
    full_a = "1d4bf0f2ff60c3e8d1a9b0c1d2e3f405a6b7c8d9"
    full_b = "1d4bf0f2ff60ffffd1a9b0c1d2e3f405a6b7c8d9"
    assert revisions_match(full_a, full_a)
    assert revisions_match(full_a, full_a[:12])
    assert revisions_match(full_a, full_a[:16])
    assert not revisions_match(full_b, full_a[:16])
    assert not revisions_match(full_a, "main")
    assert not revisions_match(full_a, "main")


def test_include_key_order_independent():
    assert include_key(["*Q4*", "*.gguf"]) == include_key(["*.gguf", "*Q4*"])
    assert entry_key("huggingface:acme/toy", None, ["*Q4*", "*.gguf"]) == entry_key(
        "huggingface:acme/toy", None, ["*.gguf", "*Q4*"]
    )


def test_try_parse_source_unknown_provider():
    assert try_parse_source("other:foo/bar") is None
    assert try_parse_source("huggingface:acme/toy") is not None
    with pytest.raises(SystemExit, match="cannot parse source ref"):
        try_parse_source("huggingface:Qwen-Qwen3")


def test_overlay_have_partial_want_unknown():
    catalog = _catalog([
        _entry("huggingface:acme/have", desire=6),
        _entry("huggingface:acme/partial", desire=9),
        _entry("huggingface:acme/want", desire=8),
        _entry("other:foo/bar", desire=9),
    ])
    records = [
        _record("huggingface:acme/have"),
        _record("huggingface:acme/partial", partial=True, remaining=400),
    ]
    rows = overlay(catalog, records, progress=lambda *a, **k: None)
    by_src = {r["source"]: r["status"] for r in rows}
    assert by_src["huggingface:acme/have"] == "have"
    assert by_src["huggingface:acme/partial"] == "partial"
    assert by_src["huggingface:acme/want"] == "want"
    assert by_src["other:foo/bar"] == "unknown"
    wanted = filter_want(rows)
    assert all(r["status"] in ("want", "partial") for r in wanted)
    assert next_entry(rows, desire=True)["source"] == "huggingface:acme/partial"
    nxt = next_entry(rows, desire=True)
    assert nxt["status"] != "unknown"


def test_next_entry_prefers_partial_over_higher_desire_want():
    rows = [
        {"status": "want", "desire": 9, "source": "huggingface:acme/want"},
        {"status": "partial", "desire": 1, "source": "huggingface:acme/partial", "remaining_bytes": 10},
        {"status": "unknown", "desire": 9, "source": "other:foo/bar"},
    ]
    assert next_entry(rows, desire=True)["source"] == "huggingface:acme/partial"
    ordered = sort_rows(rows, "next")
    assert [r["source"] for r in ordered] == [
        "huggingface:acme/partial",
        "huggingface:acme/want",
        "other:foo/bar",
    ]


def test_overlay_subset_is_different_work():
    catalog = _catalog([_entry("huggingface:acme/toy")])
    records = [_record("huggingface:acme/toy", include=["*Q4*"])]
    rows = overlay(catalog, records)
    assert rows[0]["status"] == "want"


def test_overlay_null_revision_matches_any_pin():
    catalog = _catalog([_entry("huggingface:acme/toy", revision=None)])
    records = [_record("huggingface:acme/toy", revision="bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb")]
    rows = overlay(catalog, records)
    assert rows[0]["status"] == "have"


def test_sort_rows_desire_size_name_status():
    rows = [
        {"status": "have", "desire": 3, "source": "b", "remaining_bytes": 0},
        {"status": "want", "desire": 9, "source": "a", "remaining_bytes": 50},
        {"status": "partial", "desire": None, "source": "c", "remaining_bytes": 10},
        {"status": "unknown", "desire": 1, "source": "d", "remaining_bytes": None},
    ]
    assert [r["source"] for r in sort_rows(rows, "desire")] == ["a", "b", "d", "c"]
    # SIZE sort is remaining-to-fetch (None keys as if remaining were -1).
    assert [r["source"] for r in sort_rows(rows, "size")] == ["a", "c", "b", "d"]
    assert [r["source"] for r in sort_rows(rows, "name")] == ["a", "b", "c", "d"]
    assert [r["source"] for r in sort_rows(rows, "status")] == ["b", "c", "a", "d"]
    assert [r["source"] for r in sort_rows(rows, "nope")] == ["b", "a", "c", "d"]


def test_sort_next_puts_unfinished_first():
    rows = [
        {"status": "have", "desire": 6, "source": "a"},
        {"status": "want", "desire": 3, "source": "b"},
        {"status": "partial", "desire": 9, "source": "c"},
        {"status": "unknown", "desire": 9, "source": "d"},
    ]
    ordered = sort_rows(rows, "next")
    assert [r["source"] for r in ordered] == ["c", "b", "a", "d"]


def test_next_entry_vault_is_largest_partial():
    rows = [
        {"status": "partial", "remaining_bytes": 10, "source": "small", "desire": 9},
        {"status": "partial", "remaining_bytes": 50, "source": "big", "desire": 1},
        {"status": "want", "remaining_bytes": 999, "source": "want", "desire": 9},
    ]
    assert next_entry(rows, desire=False)["source"] == "big"


def test_realize_from_overlay_partial_uses_matched_pin():
    row = {
        "status": "partial",
        "source": "huggingface:acme/toy",
        "revision": None,
        "include": None,
        "matched_revision": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
        "matched_include": ["*Q4*"],
    }
    source, rev, include = realize_from_overlay(row)
    assert source == "huggingface:acme/toy"
    assert rev.startswith("bbbb")
    assert include == ["*Q4*"]
    want = {"status": "want", "source": "huggingface:acme/toy", "revision": None, "include": ["*Q5*"]}
    source, rev, include = realize_from_overlay(want)
    assert rev is None
    assert include == ["*Q5*"]


def test_estimate_digest_allowlist():
    est = {
        "as_of": "2026-08-20T11:00:00+00:00",
        "artifact_type": "model",
        "source": {
            "revision": "aaa",
            "revision_ref": "main",
            "license": "apache-2.0",
            "gated": False,
        },
        "payload": {"total_size_bytes": 100, "file_count": 2, "unknown_size_count": 0},
        "parameters": {"total": 16, "by_dtype": {"F32": 16}, "dominant_dtype": "F32"},
        "disk": {"checked_path": "/Users/alex/darsay"},
        "bundle": {"dir": "/Users/alex/darsay/x"},
    }
    digest = estimate_digest(est)
    assert set(digest) == DIGEST_KEYS
    assert digest["parameters"] == 16
    assert digest["dominant_dtype"] == "F32"
    dumped = json.dumps(digest)
    assert "checked_path" not in dumped
    assert "precision" not in digest


def test_estimate_is_stale():
    from datetime import datetime, timezone, timedelta
    now = datetime(2026, 8, 26, tzinfo=timezone.utc)
    fresh = (now - timedelta(days=2)).isoformat()
    old = (now - timedelta(days=12)).isoformat()
    assert not estimate_is_stale(fresh, now=now)
    assert estimate_is_stale(old, now=now)


def test_new_catalog_casefold(tmp_path):
    cat = new_catalog(tmp_path, "Summer", title="Summer 2026")
    assert cat["id"] == "summer"
    assert (tmp_path / "catalogs" / "summer" / "catalog.json").is_file()
    assert (tmp_path / "catalogs" / "summer" / "curation.md").is_file()
    path = resolve_catalog(tmp_path, "SUMMER")
    assert path.parent.name == "summer"
    assert try_resolve_catalog(tmp_path, "Summer") == path
    loaded = load_catalog(path)
    assert loaded["title"] == "Summer 2026"


def test_load_save_roundtrip_and_unknown_fields(tmp_path):
    path = tmp_path / "catalog.json"
    raw = _catalog([_entry("huggingface:acme/toy", desire=5)])
    raw["future_field"] = 1
    path.write_text(json.dumps(raw), encoding="utf-8")
    loaded = load_catalog(path)
    assert loaded["id"] == "summer"
    assert loaded["entries"][0]["desire"] == 5
    assert loaded["future_field"] == 1
    save_catalog(path, loaded)
    again = json.loads(path.read_text(encoding="utf-8"))
    assert again["kind"] == CATALOG_KIND
    assert again["entries"][0]["source"] == "huggingface:acme/toy"
    assert again["future_field"] == 1


def test_save_projects_estimate_disk_paths(tmp_path):
    path = tmp_path / "catalog.json"
    live = {
        "as_of": "2026-08-20T11:00:00+00:00",
        "artifact_type": "model",
        "source": {
            "revision": "aaa",
            "revision_ref": "main",
            "license": "apache-2.0",
            "gated": False,
        },
        "payload": {"total_size_bytes": 100, "file_count": 2, "unknown_size_count": 0},
        "parameters": {"total": 16, "by_dtype": {"F32": 16}, "dominant_dtype": "F32"},
        "disk": {"checked_path": "/Users/alex/darsay"},
        "bundle": {"dir": "/Users/alex/darsay/x"},
    }
    raw = _catalog([_entry("huggingface:acme/toy", estimate=live)])
    path.write_text(json.dumps(raw), encoding="utf-8")
    loaded = load_catalog(path)
    est = loaded["entries"][0]["estimate"]
    assert est["payload_bytes"] == 100
    assert "checked_path" not in est
    save_catalog(path, loaded)
    dumped = path.read_text(encoding="utf-8")
    assert "checked_path" not in dumped
    assert "/Users/alex" not in dumped


def test_load_rejects_known_provider_typo(tmp_path):
    path = tmp_path / "catalog.json"
    raw = _catalog([_entry("huggingface:Qwen-Qwen3")])
    path.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(SystemExit, match="cannot parse source ref"):
        load_catalog(path)


def test_load_rejects_unknown_major(tmp_path):
    path = tmp_path / "catalog.json"
    raw = _catalog([])
    raw["catalog_schema_version"] = "2.0.0"
    path.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(SystemExit, match="newer than this darsay"):
        load_catalog(path)


def test_load_rejects_missing_version(tmp_path):
    path = tmp_path / "catalog.json"
    path.write_text(json.dumps({"kind": CATALOG_KIND, "id": "x"}), encoding="utf-8")
    with pytest.raises(SystemExit, match="catalog_schema_version missing"):
        load_catalog(path)


def test_upsert_and_drop_include_set(tmp_path):
    cat = new_catalog(tmp_path, "summer")
    upsert_entry(cat, "huggingface:acme/toy", desire=3, include=["*Q4*", "*.gguf"])
    entry, action = upsert_entry(cat, "huggingface:acme/toy", desire=9, include=["*.gguf", "*Q4*"])
    assert action == "updated"
    assert entry["desire"] == 9
    assert len(cat["entries"]) == 1
    upsert_entry(cat, "huggingface:acme/toy", desire=5, include=["*Q5*"])
    assert len(cat["entries"]) == 2
    _, action = upsert_entry(cat, "huggingface:acme/toy", desire=5, include=["*Q5*"])
    assert action == "unchanged"
    with pytest.raises(SystemExit, match="matches 2 entries"):
        drop_entry(cat, "huggingface:acme/toy")
    drop_entry(cat, "huggingface:acme/toy", include=["*Q5*"], include_given=True)
    assert len(cat["entries"]) == 1
    upsert_entry(cat, "huggingface:acme/toy", desire=1)
    assert len(cat["entries"]) == 2
    with pytest.raises(SystemExit, match="--full"):
        drop_entry(cat, "huggingface:acme/toy")
    drop_entry(cat, "huggingface:acme/toy", include=None, include_given=True)
    assert len(cat["entries"]) == 1
    assert include_key(cat["entries"][0].get("include")) == include_key(["*Q4*", "*.gguf"])


def test_upsert_skips_unknown_provider_rows():
    dest = _catalog([_entry("other:foo/bar", desire=9)])
    entry, action = upsert_entry(dest, "huggingface:acme/toy", desire=8)
    assert action == "added"
    assert entry["source"] == "huggingface:acme/toy"
    assert [e["source"] for e in dest["entries"]] == ["other:foo/bar", "huggingface:acme/toy"]


def test_adopt_skips_existing():
    dest = _catalog([_entry("huggingface:acme/toy", desire=2)])
    other = _catalog([
        _entry("huggingface:acme/toy", desire=9),
        _entry("huggingface:acme/other", desire=8),
    ])
    adopted, skipped = adopt_entries(dest, other)
    assert adopted == 1
    assert skipped == 1
    assert dest["entries"][0]["desire"] == 2


def test_fold_slug():
    assert fold_slug("Summer") == "summer"
    assert fold_slug(" SUMMER ") == "summer"


def test_path_resolve(tmp_path):
    cat = new_catalog(tmp_path, "summer")
    path = Path(cat["_path"])
    assert try_resolve_catalog(tmp_path, str(path)) == path
    assert try_resolve_catalog(tmp_path, str(path.parent)) == path
    assert try_resolve_catalog(tmp_path, "nope") is None


def test_slug_does_not_resolve_cwd_dir(tmp_path, monkeypatch):
    vault_cat = new_catalog(tmp_path, "summer")
    work = tmp_path / "work"
    clone = work / "summer"
    clone.mkdir(parents=True)
    (clone / "catalog.json").write_text(
        json.dumps(_catalog([_entry("huggingface:acme/clone")])),
        encoding="utf-8",
    )
    monkeypatch.chdir(work)
    assert try_resolve_catalog(tmp_path, "summer") == Path(vault_cat["_path"])
    found = try_resolve_catalog(tmp_path, "./summer")
    assert found is not None
    assert found.resolve() == (clone / "catalog.json").resolve()
    assert found.resolve() != Path(vault_cat["_path"]).resolve()


def test_format_archive_command_quotes_include():
    line = format_archive_command(
        "huggingface:acme/toy",
        revision="bbbbbbbbbbbb",
        include=["*config.json"],
        vault="/srv/vault",
    )
    assert line.startswith("darsay --vault /srv/vault archive huggingface:acme/toy")
    assert "--revision bbbbbbbbbbbb" in line
    assert "--include" in line
    assert "*config.json" in line


def test_next_idle_message_empty_vs_complete():
    empty = _catalog([])
    msg, err = next_idle_message(empty, [])
    assert err
    assert "is empty" in msg
    assert "catalog add" in msg
    rows = overlay(_catalog([_entry("huggingface:acme/toy")]), [_record("huggingface:acme/toy")])
    msg, err = next_idle_message(_catalog([_entry("huggingface:acme/toy")]), rows)
    assert not err
    assert "nothing missing" in msg
    unknown_rows = overlay(
        _catalog([_entry("other:foo/bar")]),
        [],
        progress=lambda *a, **k: None,
    )
    msg, err = next_idle_message(_catalog([_entry("other:foo/bar")]), unknown_rows)
    assert err
    assert "unknown source provider" in msg


def test_write_catalog_readme_includes_include_cached_size_and_overlay_hints(tmp_path):
    dest = tmp_path / "summer"
    dest.mkdir()
    catalog = _catalog(
        [_entry(
            "huggingface:acme/toy",
            desire=8,
            include=["*Q4_K_M*"],
            note="the quant",
            estimate={
                "payload_bytes": 1024,
                "as_of": "2026-08-01T00:00:00+00:00",
                "artifact_type": "model",
                "license": "apache-2.0",
            },
        )],
        curator="Alex",
        note="a want-list",
    )
    write_catalog_readme(dest, catalog)
    text = (dest / "README.md").read_text(encoding="utf-8")
    assert "A darsay catalog (`summer`)" in text
    assert "Curator: Alex" in text
    assert "> a want-list" in text
    assert "*Q4_K_M*" in text
    assert "1.0 KiB" in text
    assert "apache-2.0" in text
    assert "the quant" in text
    assert "darsay list ./catalog.json" in text
    assert "darsay archive --next ./catalog.json" in text


def test_print_catalog_index(capsys):
    print_catalog_index([
        {
            "id": "summer",
            "title": "Summer 2026",
            "curator": None,
            "entries": [{}, {}],
            "updated": "2026-08-26T12:00:00+00:00",
        }
    ])
    out = capsys.readouterr().out
    assert "CATALOG" in out
    assert "summer" in out
    assert "Summer 2026" in out
    assert "2" in out
    assert "2026-08-26" in out


def test_print_catalog_table_hides_empty_desire_note(capsys):
    print_catalog_table([
        {
            "status": "have",
            "desire": None,
            "source": "huggingface:acme/toy",
            "revision": None,
            "include": None,
            "note": None,
            "bundle_id": "acme--toy@aaaaaaaaaaaa",
            "payload_bytes": 100,
            "estimate_stale": False,
            "gated": False,
        }
    ])
    out = capsys.readouterr().out
    assert "STATUS" in out
    assert "SOURCE" in out
    assert "HAVE" in out
    assert "DESIRE" not in out
    assert "NOTE" not in out
    capsys.readouterr()
    print_catalog_table([
        {
            "status": "want",
            "desire": 8,
            "source": "huggingface:acme/toy",
            "revision": None,
            "include": None,
            "note": "keep",
            "bundle_id": None,
            "payload_bytes": None,
            "estimate_stale": False,
            "gated": False,
        }
    ])
    out = capsys.readouterr().out
    assert "DESIRE" in out
    assert "NOTE" in out


def test_vault_header_omits_remaining_when_complete(tmp_path):
    line = vault_header_line(tmp_path, {
        "have": 2, "partial": 0, "want": 0, "unknown": 0,
        "remaining_bytes": 0, "remaining_unknown": False, "on_disk_bytes": 100,
    })
    assert "remaining" not in line
    partial = vault_header_line(tmp_path, {
        "have": 1, "partial": 1, "want": 0, "unknown": 0,
        "remaining_bytes": 50, "remaining_unknown": False, "on_disk_bytes": 100,
    })
    assert "remaining" in partial


def test_overlay_stats_unknown_size_count_is_not_zero_remaining():
    rows = [{
        "status": "want",
        "on_disk_bytes": 0,
        "remaining_bytes": 0,
        "estimate": {"as_of": "2026-08-01T00:00:00+00:00", "unknown_size_count": 3},
    }]
    stats = overlay_stats(rows)
    assert stats["remaining_bytes"] == 0
    assert stats["remaining_unknown"] is True


def test_warning_detail_strips_error_prefix():
    assert warning_detail(SystemExit("error: unreadable catalog at x")) == "unreadable catalog at x"
    assert warning_detail(SystemExit("nope")) == "nope"


def test_project_stored_estimate_none():
    assert project_stored_estimate(None) is None
    assert project_stored_estimate("x") is None
