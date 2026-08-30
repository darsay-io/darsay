"""darsay — keep a model forever, run it tomorrow.

    pipx install darsay
    darsay archive Qwen/Qwen3-0.6B
    darsay run     qwen--qwen3-0.6b Say hello

A vault is a folder of bundles. A bundle is one pinned revision:
immutable payload, recorded facts, still loadable as-is.

    darsay estimate huggingface:Qwen/Qwen3-0.6B   preflight: size, params, disk — no download
    darsay estimate datasets/<owner>/<name>       Hugging Face shorthand; same for a dataset
    darsay archive huggingface:Qwen/Qwen3-0.6B    download + hash + manifest + reports
    darsay archive datasets/<owner>/<name>        archive a dataset (payload under data/)
    darsay verify  <bundle>           re-hash and compare against manifest
    darsay smoke   <bundle> [--inference]
    darsay list [--json]              vault as a catalog view (STATUS / SOURCE / HAVE)
    darsay list CATALOG               overlay a catalog on this vault
    darsay catalog new NAME           start a shareable want-list
    darsay archive --next CATALOG     fetch the next unfinished catalog entry
    darsay rm      <bundle> […]       delete bundles (confirmation unless --yes)
    darsay du                         disk usage of bundles and .runtime
    darsay config                     effective settings and the files that set them
    darsay complete bash|zsh|fish     print a completion script to eval
    darsay info    <bundle>           quick manifest summary
    darsay regen   <bundle>           rebuild README.md after editing curation.md
    darsay export  <bundle> [-o DIR]  pack into a single deterministic .mvb.tar
    darsay import  <file.mvb.tar>     unpack + verify into the vault
    darsay assemble <partial> […]     combine matching partials offline
    darsay hydrate <bundle>           build a runnable env for the bundle
    darsay run     <bundle> [PROMPT]  hydrate if needed, then generate (offline)
    darsay run     <bundle> --repl    interactive; quotes around the prompt are optional
    darsay dehydrate <bundle>         drop the bundle's hydration record
    darsay envs [--prune]             list / clean up shared runtime envs

<bundle> is a path, a bundle id from `list` (name@revision12), or a unique prefix.

Source refs are provider-qualified (`huggingface:Qwen/Qwen3-0.6B`,
`huggingface:datasets/<owner>/<name>`). Unprefixed `owner/name`,
`datasets/owner/name`, and huggingface.co URLs are Hugging Face shorthand.
Bundle-path commands dispatch on the manifest's artifact_type.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import sys
from pathlib import Path

from . import __version__


def _vault_path(args, *, announce: bool = False) -> Path:
    from .vault import announce_vault, default_vault, using_implicit_vault

    if args.vault:
        path = Path(args.vault).expanduser()
    else:
        path = default_vault()
    if announce:
        announce_vault(path, implicit=using_implicit_vault(args.vault))
    return path


def _bundle_dir(
    args, spec: str | None = None, *, require_manifest: bool = True
) -> Path:
    from .vault import resolve_bundle

    return resolve_bundle(
        _vault_path(args),
        spec if spec is not None else args.bundle,
        require_manifest=require_manifest,
    )


def _positive_float(value: str) -> float:
    try:
        parsed = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            f"expected a positive number, got {value!r}"
        ) from exc
    if parsed <= 0:
        raise argparse.ArgumentTypeError(f"expected a positive number, got {value!r}")
    return parsed


def _byte_size(value: str) -> int:
    """Parse bytes with optional binary K/M/G/T suffixes (and optional B)."""
    from .config import parse_byte_size

    try:
        parsed = parse_byte_size(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc
    if parsed <= 0:
        raise argparse.ArgumentTypeError(
            f"expected a positive byte size, got {value!r}"
        )
    return parsed


def _min_free(value: str) -> int:
    """Byte size for the free-space floor; 0 disables it."""
    from .config import parse_byte_size

    try:
        return parse_byte_size(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


def _rate(value: str) -> int:
    """Bytes per second for the transfer cap (``5M``, ``5M/s``); 0 lifts it."""
    from .config import parse_rate

    try:
        return parse_rate(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


def _duration(value: str) -> float:
    """Seconds from ``30s`` / ``15m`` / ``1h`` / ``2d`` or a bare number; 0 allowed."""
    from .config import parse_duration

    try:
        return parse_duration(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


def _positive_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            f"expected a positive integer, got {value!r}"
        ) from exc
    if parsed <= 0:
        raise argparse.ArgumentTypeError(f"expected a positive integer, got {value!r}")
    return parsed


def _shard_key(value: str) -> tuple[int, int]:
    """Parse the one-based cooperative transfer key N/T."""
    match = re.fullmatch(r"\s*(\d+)\s*/\s*(\d+)\s*", value)
    if not match:
        raise argparse.ArgumentTypeError("expected N/T, for example 1/3")
    participant, total = (int(part) for part in match.groups())
    if total < 2:
        raise argparse.ArgumentTypeError("the total in N/T must be at least 2")
    if total > 1024:
        raise argparse.ArgumentTypeError("the total in N/T cannot exceed 1024")
    if participant < 1 or participant > total:
        raise argparse.ArgumentTypeError("N/T requires 1 <= N <= T")
    return participant, total


def _desire(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("desire must be an integer 1–9") from exc
    if parsed < 1 or parsed > 9:
        raise argparse.ArgumentTypeError("desire must be an integer 1–9")
    return parsed


def cmd_estimate(args) -> int:
    from .catalog import try_resolve_catalog
    from .estimate import estimate, print_estimate

    vault = _vault_path(args, announce=True)
    target = getattr(args, "target", None) or getattr(args, "source", None)
    cat_path = try_resolve_catalog(vault, target)
    if cat_path is not None:
        return _estimate_catalog(args, vault, cat_path)
    if args.entry:
        raise SystemExit(
            "error: a second argument is only valid when TARGET is a catalog"
        )
    est = estimate(
        target,
        revision=args.revision,
        vault=vault,
        include=args.include,
        variants=args.variants,
        progress=(lambda *a: None) if args.json else print,
    )
    if args.json:
        print(json.dumps(est, indent=2, ensure_ascii=False))
    else:
        print_estimate(est)
    return 1 if est["disk"]["verdict"] == "insufficient" else 0


def _estimate_catalog(args, vault, cat_path) -> int:
    from .archiver import utc_now
    from .catalog import (
        adopt_resolved_source,
        estimate_digest,
        load_catalog,
        overlay,
        overlay_stats,
        require_writable,
        save_catalog,
        try_parse_source,
        warning_detail,
        write_catalog_readme,
    )
    from .estimate import estimate
    from .readme_gen import human_params, human_size
    from .vault import bundle_records

    if args.revision or args.include:
        raise SystemExit(
            "error: those are entry fields; pass SOURCE as the second argument to refresh one row"
        )
    if args.variants:
        print("warning: --variants is ignored for catalog estimate", file=sys.stderr)
    require_writable(vault, cat_path, bool(getattr(args, "write", False)))
    catalog = load_catalog(cat_path)
    only_canonical = None
    if args.entry:
        ref = try_parse_source(args.entry)
        if ref is None:
            raise SystemExit(f"error: cannot parse source {args.entry!r}")
        only_canonical = ref.canonical
    quiet = args.json
    selected = []
    for entry in catalog["entries"]:
        parsed = try_parse_source(entry["source"])
        got = parsed.canonical if parsed is not None else entry["source"]
        if only_canonical is not None and got != only_canonical:
            continue
        selected.append(entry)
    if only_canonical is not None and not selected:
        raise SystemExit(f"error: {args.entry} is not in catalog {catalog['id']}")
    if not quiet:
        n = len(selected)
        print(
            f"Refreshing {n} estimate{'s' if n != 1 else ''} in catalog {catalog['id']} "
            "(metadata only, no download) ..."
        )
    failed = 0
    digests = []
    for entry in selected:
        parsed = try_parse_source(entry["source"])
        if parsed is None:
            print(
                f"warning: unknown source provider in {entry['source']!r}",
                file=sys.stderr,
            )
            failed += 1
            continue
        try:
            est = estimate(
                entry["source"],
                revision=entry.get("revision"),
                vault=vault,
                include=entry.get("include"),
                progress=lambda *a, **k: None,
            )
        except SystemExit as exc:
            print(f"warning: {warning_detail(exc)}", file=sys.stderr)
            failed += 1
            continue
        digest = estimate_digest(est)
        adopt_resolved_source(catalog, entry, est["source"]["address"])
        entry["estimate"] = digest
        digests.append(
            {"source": entry["source"], "include": entry.get("include"), **digest}
        )
        if quiet:
            continue
        extra = f"  [{', '.join(entry['include'])}]" if entry.get("include") else ""
        gated = "  GATED" if digest.get("gated") else ""
        params = ""
        if digest.get("parameters"):
            dtype = (
                f" {digest['dominant_dtype']}" if digest.get("dominant_dtype") else ""
            )
            params = f"  {human_params(digest['parameters'])}{dtype}"
        print(
            f"  {entry['source']}{extra}  {human_size(digest['payload_bytes'])}  "
            f"{digest.get('license') or '?'}{gated}{params}"
        )
    catalog["updated"] = utc_now()
    save_catalog(cat_path, catalog)
    write_catalog_readme(cat_path.parent, catalog)
    if args.json:
        print(json.dumps(digests, indent=2, ensure_ascii=False))
        return 1 if failed else 0
    records = bundle_records(vault)
    stats = overlay_stats(overlay(catalog, records))
    print(f"Updated {cat_path}")
    unknown = " + ?" if stats["remaining_unknown"] else ""
    print(f"  remaining (this vault): {human_size(stats['remaining_bytes'])}{unknown}")
    return 1 if failed else 0


def _tty_confirm(question: str) -> bool:
    """Ask on the terminal; Enter and ``y`` mean yes, ``n`` or EOF mean no.

    The archive's SIGINT handler swallows a first Ctrl-C (it requests a
    clean stop mid-transfer), which at a prompt would just leave the user
    waiting; the default handler is restored for the question so Ctrl-C
    aborts it.
    """
    import signal

    previous = signal.signal(signal.SIGINT, signal.default_int_handler)
    try:
        answer = input(question)
    except EOFError:
        return False
    finally:
        signal.signal(signal.SIGINT, previous)
    return answer.strip().lower() in {"", "y", "yes"}


def _on_a_terminal() -> bool:
    """Both stdin and stdout are TTYs: someone is there to answer a question."""
    try:
        return sys.stdin.isatty() and sys.stdout.isatty()
    except (AttributeError, ValueError):
        return False


def cmd_archive(args) -> int:
    from .archiver import archive
    from .transfer import PartialTransfer

    # The build that is running, so a pasted terminal identifies itself.
    print(f"darsay {__version__}", file=sys.stderr)
    vault = _vault_path(args, announce=True)
    target = _archive_target(args, vault)
    if target is None:
        return 0
    source, revision, include = target
    max_bytes = (
        int(args.max_gb * 1024**3) if args.max_gb is not None else args.max_bytes
    )
    # Ask before a transfer that cannot finish only when someone is there
    # to answer; cron and pipes proceed, as they always have.
    confirm = None if args.yes or not _on_a_terminal() else _tty_confirm
    try:
        bundle = archive(
            source,
            revision=revision,
            vault=vault,
            force=args.force,
            dry_run=args.dry_run,
            max_bytes=max_bytes,
            max_minutes=args.max_minutes,
            min_free=args.min_free,
            max_rate=args.max_rate,
            max_offline=args.max_offline,
            rehash=args.rehash,
            jobs=args.jobs,
            shard=args.shard,
            include=include,
            confirm=confirm,
        )
    except PartialTransfer as stop:
        print(f"\nArchive paused cleanly ({stop.reason}: {stop.detail}).")
        print(f"Partial bundle: {stop.bundle_dir}")
        if stop.reason == "moved":
            print(
                "Everything here is verified or already moved to another vault. "
                "Assemble the halves into one vault, then run archive there to register:"
            )
            print(
                f"  darsay --vault <vault-with-the-other-half> assemble {stop.bundle_dir}"
            )
            return 10
        action = {
            "disk": "Free disk space, then re-run",
            "offline": "Once the network is back, re-run",
        }.get(stop.reason, "Re-run")
        print(
            f"{action} the same archive command to continue from verified and partial bytes."
        )
        return 10
    except KeyboardInterrupt:
        # Outside the transfer window (e.g. during pin) nothing durable has
        # started, so a plain interrupt exit is honest.
        print("\nInterrupted.", file=sys.stderr)
        return 130
    if bundle is None:  # --dry-run printed the plan and intentionally did not register
        return 0
    bundle_id = f"{bundle.parent.name}@{bundle.name}"
    from .archiver import load_manifest

    artifact = load_manifest(bundle).get("artifact_type")
    next_cmd = (
        f"darsay info {bundle_id}"
        if artifact == "dataset"
        else f"darsay run {bundle_id}"
    )
    print(f"\nBundle ready: {bundle}")
    print(f"  id:           {bundle_id}")
    print(f"  manifest:     {bundle / 'manifest.json'}")
    print(f"  readme:       {bundle / 'README.md'}")
    print(f"  verification: {bundle / 'VERIFICATION.md'}")
    print(
        f"  curation:     {bundle / 'curation.md'}  <- edit this, then `darsay regen`"
    )
    print(f"  next:         {next_cmd}")
    return 0


def _finish_next(message: str, error: bool = False) -> int:
    """Idle ``--next``: errors raise; idempotent success prints on stderr and returns 0."""
    if error:
        raise SystemExit("error: " + message)
    print(message, file=sys.stderr)
    return 0


def _archive_target(args, vault) -> tuple[str, str | None, list[str] | None] | None:
    """Resolve archive SOURCE vs --next CATALOG."""
    from .catalog import (
        load_catalog,
        next_entry,
        next_idle_message,
        overlay,
        realize_from_overlay,
        resolve_catalog,
        try_resolve_catalog,
    )
    from .vault import bundle_records

    next_flag = getattr(args, "next", None)
    source = args.source
    if next_flag is None:
        if not source:
            raise SystemExit("error: archive requires a source (or --next CATALOG)")
        if try_resolve_catalog(vault, source) is not None:
            raise SystemExit(
                f"error: {source!r} is a catalog, not a source\n"
                f"  hint: darsay archive --next {source}\n"
                f"  hint: darsay list {source} --want\n"
                f"  hint: darsay archive huggingface:owner/name"
            )
        return source, args.revision, args.include
    if args.revision or args.include:
        raise SystemExit(
            "error: --next already applies the entry’s revision/include; drop --revision/--include"
        )
    if next_flag != "" and source:
        raise SystemExit(
            "error: --next already chose the catalog; do not also pass SOURCE"
        )
    catalog_spec = source if next_flag == "" else next_flag
    if not catalog_spec:
        raise SystemExit("error: --next requires a catalog")
    cat_path = resolve_catalog(vault, catalog_spec)
    catalog = load_catalog(cat_path)
    rows = overlay(catalog, bundle_records(vault))
    nxt = next_entry(rows, desire=True)
    if nxt is None:
        _finish_next(*next_idle_message(catalog, rows))
        return None
    source, revision, include = realize_from_overlay(nxt)
    if nxt.get("status") == "partial":
        rev12 = (revision or "")[:12]
        pct = f" {nxt['percent']}%" if nxt.get("percent") is not None else ""
        print(
            f"Resuming from catalog {catalog['id']} "
            f"(desire {nxt.get('desire') or '—'}, partial{pct}): {source} @ {rev12}…"
        )
    else:
        extra = ""
        if include:
            extra = f"  include={','.join(include)}"
        print(
            f"Next from catalog {catalog['id']} "
            f"(desire {nxt.get('desire') or '—'}, want): {source}{extra}"
        )
    return source, revision, include


def cmd_verify(args) -> int:
    from .verify import verify_bundle

    report = verify_bundle(_bundle_dir(args))
    return 0 if report["result"] == "pass" else 1


def cmd_smoke(args) -> int:
    from .smoke import run_smoke

    results = run_smoke(_bundle_dir(args), inference=args.inference)
    failed = any(r.get("status") == "fail" for r in results.values())
    print(json.dumps(results, indent=2))
    return 1 if failed else 0


def cmd_list(args) -> int:
    from .vault import bundle_records

    machine = args.json or args.ids or getattr(args, "next", False)
    vault = _vault_path(args, announce=not machine)
    records = bundle_records(vault)
    catalog_spec = getattr(args, "catalog", None)
    if catalog_spec:
        return _list_catalog(args, vault, records, catalog_spec)
    return _list_vault(args, vault, records)


def _list_vault(args, vault, records) -> int:
    from .catalog import (
        format_archive_command,
        next_entry,
        overlay_stats,
        print_catalog_table,
        realize_from_overlay,
        sort_rows,
        vault_as_rows,
        vault_header_line,
    )

    sort = getattr(args, "sort", None)
    if sort in ("desire", "next"):
        raise SystemExit("error: --sort desire / --sort next requires a catalog")
    if args.json:
        print(json.dumps(records, indent=2, ensure_ascii=False))
        return 0
    if args.ids:
        for rec in records:
            print(rec["bundle_id"])
        return 0
    all_rows = vault_as_rows(records)
    rows = all_rows
    if getattr(args, "want", False):
        rows = [r for r in rows if r.get("status") == "partial"]
    if getattr(args, "next", False):
        nxt = next_entry(all_rows, desire=False)
        if nxt is None:
            return _finish_next("nothing in progress in the vault", error=False)
        source, revision, include = realize_from_overlay(nxt)
        print(format_archive_command(source, revision, include, vault=args.vault))
        return 0
    if not all_rows:
        print(f"No bundles in {vault}/")
        return 0
    if not rows:
        print("nothing in progress in the vault")
        return 0
    rows = sort_rows(rows, sort or "name")
    stats = overlay_stats(rows)
    print_catalog_table(rows, header_line=vault_header_line(vault, stats))
    return 0


def _list_catalog(args, vault, records, spec) -> int:
    from .catalog import (
        catalog_header_line,
        filter_want,
        format_archive_command,
        load_catalog,
        next_entry,
        next_idle_message,
        overlay,
        overlay_envelope,
        overlay_stats,
        print_catalog_table,
        realize_from_overlay,
        resolve_catalog,
        sort_rows,
    )

    cat_path = resolve_catalog(vault, spec)
    catalog = load_catalog(cat_path)
    all_rows = overlay(catalog, records)
    rows = filter_want(all_rows) if getattr(args, "want", False) else all_rows
    sort = getattr(args, "sort", None) or "next"
    rows = sort_rows(rows, sort)
    if args.json:
        print(
            json.dumps(
                overlay_envelope(catalog, vault, rows),
                indent=2,
                ensure_ascii=False,
                default=str,
            )
        )
        return 0
    if args.ids:
        printable = [r for r in rows if r.get("status") != "unknown"]
        pinned = sum(1 for r in printable if r.get("include") or r.get("revision"))
        for row in printable:
            print(row["source"])
        if pinned:
            print(
                f"warning: {pinned} of {len(printable)} refs "
                f"{'is' if pinned == 1 else 'are'} a subset or pinned entry; "
                "`archive` of that line will not apply --include/--revision.",
                file=sys.stderr,
            )
            print("  hint: darsay list " + spec + " --json", file=sys.stderr)
            print("  hint: darsay archive --next " + spec, file=sys.stderr)
        return 0
    if getattr(args, "next", False):
        nxt = next_entry(all_rows, desire=True)
        if nxt is None:
            return _finish_next(*next_idle_message(catalog, all_rows))
        source, revision, include = realize_from_overlay(nxt)
        print(format_archive_command(source, revision, include, vault=args.vault))
        return 0
    if not catalog["entries"]:
        print(
            f"Catalog {catalog['id']}  ·  {catalog.get('title') or catalog['id']}  ·  0 sources"
        )
        print()
        print("No entries. Add one:")
        print(f"  darsay catalog add {catalog['id']} huggingface:owner/name --desire 8")
        return 0
    if not rows:
        message, is_error = next_idle_message(catalog, all_rows)
        if is_error:
            raise SystemExit("error: " + message)
        print(message)
        return 0
    stats = overlay_stats(rows)
    print_catalog_table(rows, header_line=catalog_header_line(catalog, stats, rows))
    return 0


def cmd_catalog(args) -> int:
    from .catalog import iter_catalogs, print_catalog_index

    vault = _vault_path(args, announce=not getattr(args, "ids", False))
    catalogs = iter_catalogs(vault)
    if getattr(args, "ids", False):
        for cat in catalogs:
            print(cat["id"])
        return 0
    if not catalogs:
        from .catalog import catalogs_dir

        print(f"No catalogs in {catalogs_dir(vault)}/")
        print("  hint: darsay catalog new summer")
        return 0
    print_catalog_index(catalogs)
    return 0


def cmd_catalog_new(args) -> int:
    from .catalog import new_catalog

    vault = _vault_path(args, announce=True)
    catalog = new_catalog(
        vault,
        args.name,
        title=args.title,
        curator=args.curator,
        note=args.note,
    )
    dest = Path(catalog["_path"]).parent
    print(f"Catalog ready: {dest}")
    print(f"  id:       {catalog['id']}")
    print(f"  catalog:  {dest / 'catalog.json'}")
    print(f"  readme:   {dest / 'README.md'}")
    print(
        f"  curation: {dest / 'curation.md'}  <- edit this, then `darsay catalog regen`"
    )
    print(
        f"  next:     darsay catalog add {catalog['id']} huggingface:owner/name --desire 8"
    )
    return 0


def cmd_catalog_add(args) -> int:
    from .catalog import (
        estimate_digest,
        load_catalog,
        require_writable,
        resolve_catalog,
        save_catalog,
        upsert_entry,
        write_catalog_readme,
    )
    from .estimate import estimate
    from .readme_gen import human_size

    vault = _vault_path(args, announce=True)
    path = resolve_catalog(vault, args.catalog)
    require_writable(vault, path, bool(args.write))
    catalog = load_catalog(path)
    source = args.source
    digest = None
    extra = ""
    if getattr(args, "estimate", False):
        est = estimate(
            source,
            revision=args.revision,
            vault=vault,
            include=args.include,
            progress=print,
        )
        source = est["source"]["address"]
        digest = estimate_digest(est)
        gated = "  GATED" if digest.get("gated") else ""
        extra = f"  {human_size(digest['payload_bytes'])}{gated}  (as of {digest['as_of'][:10]})"
    entry, action = upsert_entry(
        catalog,
        source,
        desire=args.desire,
        note=args.note,
        revision=args.revision,
        include=args.include,
    )
    if digest is not None:
        entry["estimate"] = digest
        action = "updated" if action == "unchanged" else action
    elif action == "unchanged":
        inc = f"  include={','.join(entry['include'])}" if entry.get("include") else ""
        print(f"Unchanged {entry['source']}{inc}")
        return 0
    save_catalog(path, catalog)
    write_catalog_readme(path.parent, catalog)
    inc = f"  include={','.join(entry['include'])}" if entry.get("include") else ""
    desire = f"  desire={entry['desire']}" if entry.get("desire") is not None else ""
    verb = "Added" if action == "added" else "Updated"
    if action == "added":
        hint = extra or "  (no estimate yet; darsay estimate " + catalog["id"] + ")"
        print(f"{verb} {entry['source']}{inc}{desire}{hint}")
    else:
        print(f"{verb} {entry['source']}{inc}{desire}{extra}")
    return 0


def cmd_catalog_drop(args) -> int:
    from .catalog import (
        drop_entry,
        load_catalog,
        require_writable,
        resolve_catalog,
        save_catalog,
        write_catalog_readme,
    )

    vault = _vault_path(args, announce=True)
    path = resolve_catalog(vault, args.catalog)
    require_writable(vault, path, bool(args.write))
    catalog = load_catalog(path)
    if args.full and args.include:
        raise SystemExit("error: pass --full or --include, not both")
    removed = drop_entry(
        catalog,
        args.source,
        revision=args.revision,
        include=None if args.full else args.include,
        include_given=bool(args.include) or bool(args.full),
        revision_given=bool(args.revision),
    )
    save_catalog(path, catalog)
    write_catalog_readme(path.parent, catalog)
    print(f"Dropped {removed['source']} from {catalog['id']}")
    return 0


def cmd_catalog_adopt(args) -> int:
    from .catalog import (
        adopt_entries,
        load_catalog,
        require_writable,
        resolve_catalog,
        save_catalog,
        write_catalog_readme,
    )

    vault = _vault_path(args, announce=True)
    dest_path = resolve_catalog(vault, args.catalog)
    require_writable(
        vault,
        dest_path,
        bool(args.write),
        hint="adopt into a vault catalog; then list it",
    )
    src_path = resolve_catalog(vault, args.other)
    dest = load_catalog(dest_path)
    other = load_catalog(src_path)
    adopted, skipped = adopt_entries(dest, other)
    save_catalog(dest_path, dest)
    write_catalog_readme(dest_path.parent, dest)
    print(
        f"Adopted {adopted} entries from {other['id']} → {dest['id']} "
        f"({skipped} already present)"
    )
    print(f"  next: darsay list {dest['id']}")
    return 0


def cmd_catalog_regen(args) -> int:
    from .archiver import utc_now
    from .catalog import (
        load_catalog,
        require_writable,
        resolve_catalog,
        save_catalog,
        write_catalog_readme,
    )

    vault = _vault_path(args, announce=True)
    path = resolve_catalog(vault, args.catalog)
    require_writable(vault, path, bool(args.write))
    catalog = load_catalog(path)
    catalog["updated"] = utc_now()
    save_catalog(path, catalog)
    write_catalog_readme(path.parent, catalog)
    print(f"Regenerated {path.parent / 'README.md'}")
    return 0


def cmd_rm(args) -> int:
    import shutil

    bundles = []
    for spec in args.bundles:
        bundles.append(_bundle_dir(args, spec, require_manifest=False))
    # Unique, keep order.
    seen = set()
    unique = []
    for bundle in bundles:
        resolved = bundle.resolve()
        if resolved not in seen:
            seen.add(resolved)
            unique.append(bundle)
    vault = _vault_path(args).resolve()
    for bundle in unique:
        try:
            bundle.resolve().relative_to(vault)
        except ValueError:
            raise SystemExit(
                f"error: {bundle} is not inside the vault {vault} — refusing to delete"
            ) from None
        if bundle.resolve() == vault:
            raise SystemExit("error: refusing to delete the vault root")
    if not args.yes:
        print("Will remove:")
        for bundle in unique:
            print(f"  {bundle}")
        try:
            answer = input("Type yes to confirm: ")
        except EOFError:
            answer = ""
        if answer.strip().lower() != "yes":
            print("Aborted.")
            return 1
    for bundle in unique:
        shutil.rmtree(bundle)
        parent = bundle.parent
        try:
            if parent.is_dir() and not any(parent.iterdir()):
                parent.rmdir()
        except OSError:
            pass
        print(f"Removed {bundle}")
    return 0


def cmd_du(args) -> int:
    from .readme_gen import human_size
    from .vault import bundle_records, dir_size

    vault = _vault_path(args, announce=not args.json)
    records = bundle_records(vault)
    runtime = Path(os.environ.get("DARSAY_RUNTIME") or vault / ".runtime")
    runtime_bytes = dir_size(runtime)
    bundles_bytes = sum(r.get("on_disk_bytes") or 0 for r in records)
    payload = {
        "vault": str(vault),
        "bundles": [
            {
                "bundle_id": r["bundle_id"],
                "path": r["path"],
                "on_disk_bytes": r.get("on_disk_bytes") or 0,
                "payload_bytes": r.get("payload_bytes"),
                "partial": r.get("partial", False),
            }
            for r in records
        ],
        "bundles_bytes": bundles_bytes,
        "runtime": str(runtime),
        "runtime_bytes": runtime_bytes,
        "total_bytes": bundles_bytes + runtime_bytes,
    }
    if args.json:
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        return 0
    if not records and runtime_bytes == 0:
        print(f"No bundles in {vault}/")
        return 0
    print(f"Vault {vault}")
    for rec in records:
        note = "  (partial)" if rec.get("partial") else ""
        print(
            f"  {human_size(rec.get('on_disk_bytes') or 0):>10}  {rec['bundle_id']}{note}"
        )
    print(f"  {human_size(runtime_bytes):>10}  .runtime")
    print(f"  {human_size(payload['total_bytes']):>10}  total")
    return 0


def cmd_config(args) -> int:
    from .config import (
        SETTINGS,
        resolved_settings,
        user_config_path,
        vault_config_path,
    )

    vault = _vault_path(args, announce=not args.json)
    files = {
        "user": user_config_path(),
        "vault": vault_config_path(vault),
    }
    resolved = resolved_settings(vault)
    if args.json:
        payload = {
            "files": {
                label: {"path": str(path), "present": path.is_file()}
                for label, path in files.items()
            },
            "settings": {
                item.name: {
                    **resolved[(item.table, item.key)],
                    "default": item.default,
                    "env": item.env,
                    "flag": item.flag,
                    "help": item.help,
                }
                for item in SETTINGS
            },
        }
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        return 0
    for label, path in files.items():
        note = "" if path.is_file() else "  (not present)"
        print(f"{label + ' config:':<14} {path}{note}")
    print("Effective settings:")
    for item in SETTINGS:
        info = resolved[(item.table, item.key)]
        print(f"  {item.name} = {item.render(info['value'])}  [{info['origin']}]")
        print(f"    {item.help}")
        overrides = [f"file: [{item.table}] {item.key} = {item.example}"]
        if item.env:
            overrides.append(f"env: ${item.env}")
        if item.flag:
            overrides.append(f"flag: {item.flag}")
        print("    " + "   ".join(overrides))
    return 0


def cmd_complete(args) -> int:
    from .complete import script_for

    text = script_for(args.shell)
    sys.stdout.write(text if text.endswith("\n") else text + "\n")
    return 0


def cmd_info(args) -> int:
    from .archiver import load_manifest
    from .readme_gen import human_params, human_size

    bundle = _bundle_dir(args)
    m = load_manifest(bundle)
    print(f"{m['bundle_id']}  (schema v{m['schema_version']}, {m['artifact_type']})")
    print(f"  path:       {bundle}")
    src = m["source"]
    print(
        f"  source:     {src['address']} @ {src['revision'][:12]} ({src.get('provider') or src['origin']})"
    )
    print(
        f"  license:    {m['licensing']['spdx_id']}  commercial={m['licensing']['commercial_use']}"
    )
    if m["artifact_type"] == "dataset":
        dm = m["dataset_metadata"]
        fmts = ", ".join(
            f"{ext} x{d['file_count']}" for ext, d in (dm.get("formats") or {}).items()
        )
        declared = (dm.get("declared") or {}).get("example_count_total")
        print(
            f"  formats:    {fmts or '?'}  declared examples={f'{declared:,}' if declared is not None else '?'}"
        )
    else:
        meta = m["model_metadata"]
        print(
            f"  params:     {human_params(meta['parameter_count'])} {meta['precision'] or ''}  ctx={meta['context_length']}"
        )
    print(
        f"  payload:    {m['inventory']['file_count']} files, {human_size(m['inventory']['total_size_bytes'])}"
    )
    subset = src.get("subset")
    if subset:
        print(
            f"  subset:     {', '.join(subset.get('include') or [])}  "
            f"{subset.get('kept_file_count')} of {subset.get('full_file_count')} upstream files"
        )
    print(
        f"  integrity:  {m['security']['integrity_status']}  last check {m['archive']['last_integrity_check']}"
    )
    smoke = m["validation"]["smoke_tests"]
    print(
        "  smoke:      "
        + " ".join(f"{name}={r['status']}" for name, r in smoke.items())
    )
    if m["artifact_type"] != "dataset":
        from .hydrate import load_hydration

        hyd = load_hydration(bundle)
        if hyd:
            last = hyd["runs"][-1] if hyd.get("runs") else None
            if last:
                extras = [last["at"][:10]]
                if last.get("tokens_per_second") is not None:
                    extras.append(f"{last['tokens_per_second']} tok/s")
                run_note = f"last run {last['status']} ({', '.join(extras)})"
            else:
                run_note = "no runs yet"
            print(
                f"  hydration:  {hyd['engine']} in env {hyd['env']['key']} — {run_note}"
            )
        else:
            print(f"  hydration:  not hydrated (darsay hydrate {m['bundle_id']})")
    return 0


def cmd_export(args) -> int:
    from .export import export_bundle

    out = export_bundle(_bundle_dir(args), Path(args.output_dir))
    print(f"Export ready: {out}")
    return 0


def cmd_import(args) -> int:
    from .export import import_bundle

    import_bundle(Path(args.file), _vault_path(args, announce=True), force=args.force)
    return 0


def cmd_assemble(args) -> int:
    from .transfer import assemble_partials

    bundle, plan = assemble_partials(
        [Path(path) for path in args.partials],
        _vault_path(args, announce=True),
        move=args.move,
    )
    ledger = json.loads((bundle / "transfer.json").read_text(encoding="utf-8"))
    from .sources import source_from_ledger

    address = source_from_ledger(ledger).canonical
    print(f"\nCombined partial bundle: {bundle}")
    if plan["complete"]:
        print(
            "All payload files are present and verified; run archive once to register the bundle:"
        )
    else:
        print("Continue the combined transfer with:")
    print(
        f"  darsay --vault {shlex.quote(str(_vault_path(args)))} "
        f"archive {shlex.quote(address)}"
    )
    return 0


def cmd_hydrate(args) -> int:
    from .hydrate import hydrate_bundle

    record = hydrate_bundle(
        _bundle_dir(args),
        engine=args.engine,
        python=args.python,
        weights=args.weights,
        force=args.force,
        dry_run=args.dry_run,
        ignore_preflight=args.ignore_preflight,
    )
    if record.get("dry_run"):
        return 0
    return 0 if record["probe"].get("status") == "pass" else 1


def cmd_run(args) -> int:
    from .hydrate import run_bundle

    record = run_bundle(
        _bundle_dir(args),
        prompt=" ".join(args.prompt) if args.prompt else None,
        engine=args.engine,
        max_new_tokens=args.max_new_tokens,
        raw=args.raw,
        sample=args.sample,
        device=args.device,
        dtype=args.dtype,
        trust_remote_code=args.trust_remote_code,
        seed=args.seed,
        timeout=args.timeout,
        python=args.python,
        weights=args.weights,
        ignore_preflight=args.ignore_preflight,
        repl=args.repl,
    )
    return 0 if record["status"] == "pass" else 1


def cmd_dehydrate(args) -> int:
    from .hydrate import dehydrate_bundle

    dehydrate_bundle(_bundle_dir(args))
    return 0


def cmd_envs(args) -> int:
    from .hydrate import list_envs, prune_envs
    from .readme_gen import human_size

    vault = _vault_path(args)
    if args.prune:
        prune_envs(vault)
        return 0
    envs = list_envs(vault)
    if not envs:
        print(f"No runtime envs under {vault}/.runtime/envs/ (or $DARSAY_RUNTIME)")
        return 0
    for env in envs:
        used = ", ".join(env["used_by"]) or "UNREFERENCED (candidate for --prune)"
        print(
            f"{env['key']}  python {env['python']}  {human_size(env['size_bytes'])}  created {env['created_at'][:10]}"
        )
        print(f"  {env['path']}")
        print(f"  used by: {used}")
    return 0


def cmd_regen(args) -> int:
    from .archiver import load_manifest
    from .readme_gen import write_bundle_readme

    bundle = _bundle_dir(args)
    write_bundle_readme(bundle, load_manifest(bundle))
    print(f"Regenerated {bundle / 'README.md'}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    """The whole CLI — every subcommand and flag — without parsing anything.

    ``main`` parses with it; the release gate walks it so the docs can be
    held to the flags that actually ship.
    """
    vault_help = "vault root (default: $DARSAY_HOME or ~/darsay)"
    # After the subcommand, SUPPRESS so a missing flag does not overwrite
    # `darsay --vault DIR list` with None.
    vault_after = argparse.ArgumentParser(add_help=False)
    vault_after.add_argument("--vault", default=argparse.SUPPRESS, help=vault_help)

    parser = argparse.ArgumentParser(
        prog="darsay",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--version", action="version", version=f"darsay {__version__}")
    parser.add_argument("--vault", default=None, help=vault_help)
    sub = parser.add_subparsers(dest="command", required=False)

    def add_cmd(name, **kwargs):
        kwargs.setdefault("parents", [vault_after])
        return sub.add_parser(name, **kwargs)

    p = add_cmd(
        "estimate", help="preflight a source or refresh a catalog's cached sizes"
    )
    p.add_argument("target", help="source ref, catalog slug, or path to catalog.json")
    p.add_argument(
        "entry", nargs="?", help="when TARGET is a catalog, refresh only this source"
    )
    p.add_argument("--revision", help="branch, tag, or commit (default: main)")
    p.add_argument(
        "--include",
        action="append",
        metavar="GLOB",
        help="count only payload files matching GLOB (repeatable), "
        "e.g. --include '*Q4_K_M*' to size one GGUF quant of a pack repo",
    )
    p.add_argument(
        "--variants",
        action="store_true",
        help="also list upstream quantized variants of this model, with sizes",
    )
    p.add_argument("--json", action="store_true", help="machine-readable output")
    p.add_argument(
        "--write",
        action="store_true",
        help="allow writing a path-addressed catalog (vault-named catalogs are always writable)",
    )
    p.set_defaults(func=cmd_estimate)

    p = add_cmd("archive", help="download and archive a model or dataset as a bundle")
    p.add_argument(
        "source",
        nargs="?",
        help="e.g. huggingface:Qwen/Qwen3-0.6B, datasets/<owner>/<name>, or a huggingface.co URL",
    )
    p.add_argument(
        "--next",
        nargs="?",
        const="",
        metavar="CATALOG",
        help="archive the next unfinished overlay row of CATALOG",
    )
    p.add_argument(
        "--revision",
        help="branch, tag, or commit (default: main; always pinned to the resolved commit)",
    )
    p.add_argument(
        "--force", action="store_true", help="re-archive over an existing bundle"
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="pin, reconcile, and print the transfer plan without moving payload bytes",
    )
    budget = p.add_mutually_exclusive_group()
    budget.add_argument(
        "--max-gb",
        type=_positive_float,
        metavar="N",
        help="stop cleanly after approximately N GiB of network transfer",
    )
    budget.add_argument(
        "--max-bytes",
        type=_byte_size,
        metavar="SIZE",
        help="stop cleanly after network SIZE (e.g. 500M, 20G)",
    )
    p.add_argument(
        "--max-minutes",
        type=_positive_float,
        metavar="N",
        help="stop cleanly when the transfer session reaches N minutes",
    )
    p.add_argument(
        "--min-free",
        type=_min_free,
        metavar="SIZE",
        help="pause cleanly when destination free space drops below SIZE "
        "(default: 2G, or config transfer.min_free; 0 disables)",
    )
    p.add_argument(
        "-y",
        "--yes",
        action="store_true",
        help="start without asking when the disk preflight says the transfer "
        "cannot finish (a non-interactive run never asks)",
    )
    p.add_argument(
        "--max-rate",
        type=_rate,
        metavar="SIZE",
        help="cap network transfer at SIZE per second, e.g. 5M "
        "(default: unlimited, or config transfer.max_rate; 0 lifts a configured cap)",
    )
    p.add_argument(
        "--max-offline",
        type=_duration,
        metavar="DURATION",
        help="keep waiting for a lost network up to DURATION before pausing "
        "cleanly, e.g. 30m (default: 1h, or config transfer.max_offline; "
        "0 pauses at the first failure)",
    )
    p.add_argument(
        "--rehash",
        action="store_true",
        help="re-hash every present payload file instead of trusting verified ledger entries",
    )
    p.add_argument(
        "--jobs",
        type=_positive_int,
        default=4,
        metavar="N",
        help="parallel workers for files smaller than 8 MiB (default: 4)",
    )
    p.add_argument(
        "--shard",
        type=_shard_key,
        metavar="N/T",
        help="advisory cooperative order: fetch byte-balanced lane N of T first",
    )
    p.add_argument(
        "--include",
        action="append",
        metavar="GLOB",
        help="archive only payload files matching GLOB (repeatable), plus "
        "sidecar files (config, tokenizer, license, card). "
        "The manifest records the omitted upstream files",
    )
    p.set_defaults(func=cmd_archive)

    bundle_help = "path, bundle id (name@revision12 from `list`), or a unique prefix"

    p = add_cmd("verify", help="re-hash a bundle and compare against its manifest")
    p.add_argument("bundle", help=bundle_help)
    p.set_defaults(func=cmd_verify)

    p = add_cmd("smoke", help="run smoke tests on a bundle")
    p.add_argument("bundle", help=bundle_help)
    p.add_argument(
        "--inference",
        action="store_true",
        help="also load the model and generate (needs torch)",
    )
    p.set_defaults(func=cmd_smoke)

    p = add_cmd("list", help="list bundles, or overlay a catalog on this vault")
    p.add_argument(
        "catalog",
        nargs="?",
        help="catalog slug or path to catalog.json (omit = vault inventory)",
    )
    list_fmt = p.add_mutually_exclusive_group()
    list_fmt.add_argument(
        "--json", action="store_true", help="machine-readable records"
    )
    list_fmt.add_argument(
        "--ids",
        action="store_true",
        help="bundle ids (or catalog source refs), one per line",
    )
    list_fmt.add_argument(
        "--next",
        action="store_true",
        help="print a copy-pasteable archive command for the next unfinished row",
    )
    p.add_argument("--want", action="store_true", help="only want and partial rows")
    p.add_argument(
        "--sort",
        choices=("next", "desire", "size", "name", "status"),
        help="row order (default: next when a catalog is given, name for the vault)",
    )
    p.set_defaults(func=cmd_list)

    p = add_cmd("catalog", help="create and edit catalogs (shareable want-lists)")
    p.add_argument(
        "--ids", action="store_true", help="catalog ids, one per line (for completion)"
    )
    p.set_defaults(func=cmd_catalog)
    cat = p.add_subparsers(dest="catalog_command", required=False)

    def add_cat(name, **kwargs):
        kwargs.setdefault("parents", [vault_after])
        return cat.add_parser(name, **kwargs)

    include_help = (
        "archive only payload files matching GLOB (repeatable), plus "
        "sidecar files (config, tokenizer, license, card)"
    )
    n = add_cat("new", help="create a catalog in this vault")
    n.add_argument("name", help="slug (casefolded; letters, digits, '.', '_', '-')")
    n.add_argument("--title", help="human title (default: the slug)")
    n.add_argument("--curator", help="curator name")
    n.add_argument("--note", help="catalog-level note")
    n.set_defaults(func=cmd_catalog_new)

    a = add_cat(
        "add", help="add or update a source in a catalog (offline unless --estimate)"
    )
    a.add_argument("catalog", help="catalog slug or path")
    a.add_argument("source", help="source ref")
    a.add_argument("--desire", type=_desire, metavar="N", help="1–9, 9 = most desired")
    a.add_argument("--note", help="short curator note")
    a.add_argument("--revision", help="intended pin (hex prefix or tag)")
    a.add_argument("--include", action="append", metavar="GLOB", help=include_help)
    a.add_argument(
        "--estimate",
        action="store_true",
        help="also fetch Hub metadata and cache sizes",
    )
    a.add_argument(
        "--write", action="store_true", help="allow writing a path-addressed catalog"
    )
    a.set_defaults(func=cmd_catalog_add)

    d = add_cat("drop", help="remove a source from a catalog")
    d.add_argument("catalog", help="catalog slug or path")
    d.add_argument("source", help="source ref")
    d.add_argument("--revision", help="intended pin")
    d.add_argument("--include", action="append", metavar="GLOB", help=include_help)
    d.add_argument(
        "--full", action="store_true", help="select the full-repo row (include is null)"
    )
    d.add_argument(
        "--write", action="store_true", help="allow writing a path-addressed catalog"
    )
    d.set_defaults(func=cmd_catalog_drop)

    o = add_cat("adopt", help="copy missing entries from another catalog")
    o.add_argument("catalog", help="destination catalog")
    o.add_argument("other", help="source catalog slug or path")
    o.add_argument(
        "--write",
        action="store_true",
        help="allow writing a path-addressed destination",
    )
    o.set_defaults(func=cmd_catalog_adopt)

    g = add_cat(
        "regen", help="rebuild a catalog README.md from catalog.json + curation.md"
    )
    g.add_argument("catalog", help="catalog slug or path")
    g.add_argument(
        "--write", action="store_true", help="allow writing a path-addressed catalog"
    )
    g.set_defaults(func=cmd_catalog_regen)

    p = add_cmd("rm", help="delete one or more bundles from the vault")
    p.add_argument("bundles", nargs="+", metavar="BUNDLE", help=bundle_help)
    p.add_argument(
        "-y", "--yes", action="store_true", help="do not prompt for confirmation"
    )
    p.set_defaults(func=cmd_rm)

    p = add_cmd("du", help="disk usage of bundles and the shared runtime")
    p.add_argument("--json", action="store_true", help="machine-readable totals")
    p.set_defaults(func=cmd_du)

    p = add_cmd("config", help="show effective settings and which config file set them")
    p.add_argument("--json", action="store_true", help="machine-readable settings")
    p.set_defaults(func=cmd_config)

    p = add_cmd("complete", help="print a bash/zsh/fish completion script")
    p.add_argument("shell", choices=("bash", "zsh", "fish"))
    p.set_defaults(func=cmd_complete)

    p = add_cmd("info", help="summarize a bundle")
    p.add_argument("bundle", help=bundle_help)
    p.set_defaults(func=cmd_info)

    p = add_cmd(
        "regen", help="rebuild a bundle's README.md from manifest + curation.md"
    )
    p.add_argument("bundle", help=bundle_help)
    p.set_defaults(func=cmd_regen)

    p = add_cmd("hydrate", help="build (or reuse) a runnable local env for a bundle")
    p.add_argument("bundle", help=bundle_help)
    p.add_argument(
        "--engine", help="runtime engine (default: auto-detect from the payload)"
    )
    p.add_argument(
        "--python",
        help="interpreter for the env (default: $DARSAY_PYTHON or this python)",
    )
    p.add_argument(
        "--weights",
        help="payload weights file for single-file engines, e.g. model/foo.gguf",
    )
    p.add_argument(
        "--force", action="store_true", help="rebuild the env even if it exists"
    )
    p.add_argument(
        "--dry-run", action="store_true", help="show the plan without touching anything"
    )
    p.add_argument(
        "--ignore-preflight",
        action="store_true",
        help="try anyway if the architecture or RAM check fails",
    )
    p.set_defaults(func=cmd_hydrate)

    p = add_cmd(
        "run",
        help="run a prompt against a bundle (hydrates first if needed; fully offline)",
    )
    p.add_argument("bundle", help=bundle_help)
    p.add_argument(
        "prompt",
        nargs="*",
        help="prompt text (quotes optional: darsay run toy Say hello). "
        'Default: "Say hello in one short sentence."',
    )
    p.add_argument(
        "--repl",
        action="store_true",
        help="interactive loop; the model stays loaded. /quit to exit",
    )
    p.add_argument(
        "--engine", help="runtime engine (default: the hydrated one, else auto-detect)"
    )
    p.add_argument("--max-new-tokens", type=int, default=256)
    p.add_argument("--device", default="auto", help="auto | cpu | cuda | mps")
    p.add_argument(
        "--dtype",
        default="auto",
        help="transformers only: auto | float32 | bfloat16 | float16",
    )
    p.add_argument(
        "--raw", action="store_true", help="plain completion — skip the chat template"
    )
    p.add_argument(
        "--sample",
        action="store_true",
        help="sample with the model's generation defaults (default: greedy)",
    )
    p.add_argument("--seed", type=int, help="seed for --sample")
    p.add_argument(
        "--trust-remote-code",
        action="store_true",
        help="allow custom modeling code from the payload",
    )
    p.add_argument("--timeout", type=float, help="kill the run after N seconds")
    p.add_argument("--python", help="interpreter if hydration is needed")
    p.add_argument(
        "--weights", help="weights file if hydration is needed (single-file engines)"
    )
    p.add_argument(
        "--ignore-preflight",
        action="store_true",
        help="try anyway if the architecture or RAM check fails",
    )
    p.set_defaults(func=cmd_run)

    p = add_cmd(
        "dehydrate",
        help="remove a bundle's hydration record (envs are shared; prune via `envs --prune`)",
    )
    p.add_argument("bundle", help=bundle_help)
    p.set_defaults(func=cmd_dehydrate)

    p = add_cmd("envs", help="list shared runtime envs and which bundles use them")
    p.add_argument(
        "--prune", action="store_true", help="delete envs no hydrated bundle references"
    )
    p.set_defaults(func=cmd_envs)

    p = add_cmd(
        "export", help="pack a bundle into a single deterministic .mvb.tar file"
    )
    p.add_argument("bundle", help=bundle_help)
    p.add_argument(
        "-o",
        "--output-dir",
        default=".",
        help="directory for the .mvb.tar (default: cwd)",
    )
    p.set_defaults(func=cmd_export)

    p = add_cmd(
        "import", help="unpack a .mvb.tar into the vault, verifying before registering"
    )
    p.add_argument("file")
    p.add_argument(
        "--force",
        action="store_true",
        help="replace an existing bundle at the destination",
    )
    p.set_defaults(func=cmd_import)

    p = add_cmd(
        "assemble", help="combine matching partial bundles offline into this vault"
    )
    p.add_argument(
        "partials",
        nargs="+",
        metavar="BUNDLE",
        help="partial bundle directories with the same pinned revision",
    )
    p.add_argument(
        "--move",
        action="store_true",
        help=(
            "after verifying, delete each source's copied bytes and mark them "
            "moved (leave a skeleton the source can keep fetching into)"
        ),
    )
    p.set_defaults(func=cmd_assemble)

    return parser


def flags_by_command(parser: argparse.ArgumentParser | None = None) -> dict:
    """Every ``--flag`` the CLI ships, keyed by command path (``""`` is the root).

    Walks argparse's action list, subparsers included, so ``catalog add``
    is its own key.
    """
    parser = build_parser() if parser is None else parser
    found: dict[str, set[str]] = {}

    def walk(node: argparse.ArgumentParser, name: str) -> None:
        found[name] = {
            option
            for action in node._actions
            for option in action.option_strings
            if option.startswith("--")
        }
        for action in node._actions:
            if isinstance(action, argparse._SubParsersAction):
                for sub_name, sub in action.choices.items():
                    walk(sub, f"{name} {sub_name}".strip())

    walk(parser, "")
    return found


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "command", None):
        parser.print_help()
        return 0
    return _run(args.func, args)


def _run(func, args) -> int:
    """Run a subcommand; a failure ends as one line, never a stack trace.

    Bundle state on disk is authoritative, so nothing here needs a
    traceback to recover — ``DARSAY_DEBUG=1`` shows one for bug reports.
    """
    from .sources import SourceError

    try:
        return func(args)
    except SourceError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        return 130
    except Exception as exc:
        if os.environ.get("DARSAY_DEBUG"):
            raise
        print(
            f"darsay: unexpected {type(exc).__name__}: {exc} ({_raised_at(exc)})\n"
            "  Set DARSAY_DEBUG=1 to see the full traceback.",
            file=sys.stderr,
        )
        return 1


def _raised_at(exc: BaseException) -> str:
    """``file.py:line`` of the innermost darsay frame — enough for a bug report."""
    import traceback

    package = Path(__file__).resolve().parent
    # The outermost frame is ``_run`` itself; the error came from below it.
    frames = traceback.extract_tb(exc.__traceback__)[1:]
    ours = [f for f in frames if Path(f.filename).resolve().is_relative_to(package)]
    frame = (ours or frames)[-1]
    return f"{Path(frame.filename).name}:{frame.lineno}"


if __name__ == "__main__":
    sys.exit(main())
