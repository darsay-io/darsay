"""darsay — keep a model forever, run it tomorrow.

    pipx install darsay
    darsay archive Qwen/Qwen3-0.6B
    darsay run     qwen--qwen3-0.6b Say hello

A vault is a folder of bundles. A bundle is one pinned revision:
immutable payload, recorded facts, still loadable as-is.

    darsay estimate huggingface:Qwen/Qwen3-0.6B   preflight: size, params, disk — no download
    darsay estimate datasets/<owner>/<name>       Hugging Face shorthand; same for a dataset
    darsay classify <owner>/<name>    preservation evidence and retention per weight set
    darsay archive huggingface:Qwen/Qwen3-0.6B    download + hash + manifest + reports
    darsay archive datasets/<owner>/<name>        archive a dataset (payload under data/)
    darsay verify  <bundle>… | --all  re-hash and compare against manifest
    darsay smoke   <bundle> [--inference]
    darsay list [--json]              vault as a catalog view (STATUS / SOURCE / HAVE)
    darsay list CATALOG               overlay a catalog on this vault
    darsay catalog new NAME           start a shareable want-list
    darsay archive --next CATALOG     fetch the next unfinished catalog entry
    darsay estimate <board-url>       refresh a darsay.io board in place (fetch, classify, push)
    darsay archive --next <board-url> claim the board's next row; boundaries update its gauge
    darsay list <board-url>           overlay a board against this vault (read-only)
    darsay list <other-vault>         overlay a drive against this vault: new / have / differ
    darsay rm      <bundle> […]       delete bundles (confirmation unless --yes)
    darsay du                         disk usage of bundles and .runtime
    darsay config                     effective settings and the files that set them
    darsay doctor [--fix]             offline vault diagnostics + reversible repairs
    darsay complete bash|zsh|fish     print a completion script to eval
    darsay info    <bundle>           quick manifest summary
    darsay regen   <bundle>           rebuild README.md after editing curation.md
    darsay export  <bundle> [-o DIR]  pack into a single deterministic .mvb.tar
    darsay import  <file.mvb.tar>     unpack + verify into the vault (an older record is migrated as it lands)
    darsay migrate <bundle> | --all   bring an older record (manifest.json) to this darsay's schema; payload untouched
    darsay mv      <bundle>… VAULT    move registered bundles to another vault (verify, then remove); --all for the whole vault
    darsay cp      <bundle>… VAULT    copy registered bundles into another vault (verify there, keep source); --all for the whole vault
    darsay assemble <partial> […]     combine matching partials offline
    darsay hydrate <bundle>           build a runnable env for the bundle
    darsay run     <bundle> [PROMPT]  hydrate if needed, then generate (offline)
    darsay run     <bundle> --repl    interactive; quotes around the prompt are optional
    darsay dehydrate <bundle>         drop the bundle's hydration record
    darsay envs [--prune]             list / clean up shared runtime envs

<bundle> is a path, a bundle id from `list` (name@revision12), or a unique prefix.

Every command that writes takes -n / --dry-run: the same checks, the same
report in the conditional, nothing written, and the real command to paste.

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

# Classification reads a catalog refresh may spend before remaining rows
# price the full repo. Recorded in the refresh output when reached.
REFRESH_READ_BUDGET_REQUESTS = 256
REFRESH_READ_BUDGET_BYTES = 512 * 1024**2


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


def _bundle_command(
    args, verb: str, bundle_dir: Path, bundle_id: str, *trailing: str
) -> str:
    """A pasteable ``darsay … <bundle> …`` that resolves from where it is run.

    A bundle the vault lists is named by its id, with ``--vault`` when the
    vault is not the default so the id resolves; one addressed by a path
    somewhere else is named by that path, which needs no vault.
    """
    from .vault import command_prefix, registered_in

    vault = _vault_path(args)
    if registered_in(vault, bundle_dir):
        tokens = [*command_prefix(vault), verb, bundle_id, *trailing]
    else:
        tokens = ["darsay", verb, str(bundle_dir), *trailing]
    return shlex.join(tokens)


def _resolve_bundles(args, *, require_manifest: bool, verb: str) -> list[Path]:
    """The bundles a batch verb acts on.

    ``--all`` is every registered bundle in the vault (partials are not
    records and are skipped); otherwise each named spec, resolved. Refuses
    an empty invocation and ``--all`` with named bundles, so the two ways
    never half-combine.
    """
    from .vault import (
        bundle_records,
        resolve_bundle,
        using_implicit_vault,
        vault_absence,
    )

    vault = _vault_path(args)
    if getattr(args, "all", False):
        if args.bundles:
            raise SystemExit(f"error: name bundles or pass --all to {verb}, not both")
        absent = vault_absence(vault)
        if absent and not using_implicit_vault(args.vault):
            raise SystemExit(f"error: {absent}: {vault}")
        return [Path(r["path"]) for r in bundle_records(vault) if not r.get("partial")]
    if not args.bundles:
        raise SystemExit(
            f"error: {verb} needs a bundle (path, id, or unique prefix), or --all "
            "for every registered bundle in the vault"
        )
    return [
        resolve_bundle(vault, spec, require_manifest=require_manifest)
        for spec in args.bundles
    ]


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


DRY_RUN_FLAGS = ("-n", "--dry-run")


def _add_dry_run(parser, what: str) -> None:
    """``-n`` / ``--dry-run`` on a command that writes: same checks, same report."""
    parser.add_argument("-n", "--dry-run", action="store_true", help=what)


def _real_command(argv: list[str]) -> str:
    """The invocation that was typed, minus its dry-run flag — ready to paste."""
    tokens = []
    for token in argv:
        if token in DRY_RUN_FLAGS:
            continue
        if re.fullmatch(r"-[yn]{2,}", token):  # clustered short flags: -yn, -ny
            rest = token[1:].replace("n", "")
            if rest:
                tokens.append("-" + rest)
            continue
        tokens.append(token)
    return shlex.join(["darsay", *tokens])


def _dry_run_done(args, skipped: str, verb: str) -> None:
    """The two lines every dry run ends with: what did not happen, then the real command."""
    print(f"Dry run: {skipped}. To {verb}:")
    print(f"  {_real_command(getattr(args, 'argv', None) or [])}")


def _delta_note(added: int, removed: int) -> str:
    """How a regenerated file differs from the one on disk."""
    if not added and not removed:
        return "  (unchanged)"
    return f"  (+{added} -{removed} lines)"


def _entry_label(entry: dict) -> str:
    """One catalog entry the way ``catalog add`` echoes it."""
    label = entry["source"]
    if entry.get("revision"):
        label += f" @ {str(entry['revision'])[:12]}"
    if entry.get("include"):
        label += f"  include={','.join(entry['include'])}"
    if entry.get("desire") is not None:
        label += f"  desire={entry['desire']}"
    return label


def _board_target(spec):
    """(Board, fetched catalog path) when ``spec`` is a board URL, else None."""
    from .board import fetch_catalog, parse_board_url

    board = parse_board_url(spec or "")
    if board is None:
        return None
    import tempfile

    dest = Path(tempfile.mkdtemp(prefix="darsay-board-"))
    return board, fetch_catalog(board, dest)


def _push_board_progress(args, *, state, bundle_dir=None, percent=None) -> None:
    """Report an archive boundary to a claimed board row. Best-effort:
    a board that cannot be reached never fails the archive itself."""
    ctx = getattr(args, "_board_progress", None)
    if not ctx:
        return
    from .board import claim

    banked = total = None
    if bundle_dir is not None:
        try:
            from .schema import payload_root_for
            from .transfer import load_ledger, transfer_plan

            ledger = load_ledger(Path(bundle_dir))
            plan = transfer_plan(
                Path(bundle_dir) / payload_root_for(ledger["repo_type"]), ledger
            )
            total = plan["bytes"]["total"]
            banked = (
                plan["bytes"]["verified"]
                + plan["bytes"]["partial"]
                + plan["bytes"].get("handed_off", 0)
            )
            if total:
                percent = int(banked * 100 / total)
        except Exception:
            pass
    try:
        ok, _ = claim(
            ctx["board"],
            ctx["entry_id"],
            ctx["client"],
            state=state,
            percent=percent,
            banked_bytes=banked,
            total_bytes=total,
        )
        if ok:
            print(
                f"Board {ctx['board'].page_url} updated: {state}"
                + (f" ({percent}%)" if percent is not None else "")
            )
    except (SystemExit, Exception) as exc:  # noqa: BLE001 — never fail the archive
        print(f"warning: could not update the board claim ({exc})", file=sys.stderr)


def cmd_estimate(args) -> int:
    from .catalog import try_resolve_catalog
    from .estimate import estimate, print_estimate

    vault = _vault_path(args, announce=True)
    target = getattr(args, "target", None) or getattr(args, "source", None)
    board_hit = _board_target(target)
    if board_hit is not None:
        board, board_path = board_hit
        if not args.json:
            print(f"Fetched board catalog {board.page_url}")
        args.write = True
        rc = _estimate_catalog(args, vault, board_path)
        if rc == 0 and not args.dry_run:
            from .board import push_catalog

            result = push_catalog(board, board_path)
            if not args.json:
                print(
                    f"Pushed to {board.page_url}: "
                    f"{result.get('updated', 0)} updated, "
                    f"{result.get('added', 0)} added, "
                    f"{result.get('removed', 0)} removed"
                )
        return rc
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
        full=args.full,
        progress=(lambda *a: None) if args.json else print,
    )
    if args.json:
        print(json.dumps(est, indent=2, ensure_ascii=False))
    else:
        print_estimate(est)
    return 1 if est["disk"]["verdict"] == "insufficient" else 0


def cmd_classify(args) -> int:
    from .classify import classify_source, print_classification
    from .providers.huggingface import parse_base_model_tags
    from .sources import SourceError, get_provider, parse_source

    ref = parse_source(args.source)
    provider = get_provider(ref.provider)
    progress = (lambda *a, **k: None) if args.json else print
    progress(
        f"Resolving {ref.canonical} @ {args.revision or provider.default_revision} "
        "(metadata + bounded header reads, no download) ..."
    )
    try:
        snapshot = provider.pin(ref, args.revision, require_access=False)
    except SourceError as exc:
        raise SystemExit(str(exc)) from None
    if snapshot.source.canonical != ref.canonical:
        ref = snapshot.source
    if ref.artifact_type != "model":
        raise SystemExit(
            f"error: classify applies to models — {ref.canonical} is a "
            f"{ref.artifact_type}"
        )
    files = [
        {"path": f.path, "size": f.size, "sha256": f.sha256, "git_sha1": f.git_sha1}
        for f in snapshot.files
    ]
    subset = None
    if args.include:
        from .subset import select_subset

        files, subset = select_subset(files, args.include)
    tags = list((snapshot.metadata or {}).get("tags") or [])
    base_ids, _ = parse_base_model_tags(tags)
    base_locator = base_ids[0] if base_ids else None
    result = classify_source(
        provider,
        ref,
        snapshot.revision,
        files,
        base_locator=base_locator,
    )
    result["source"] = {
        "provider": ref.provider,
        "address": ref.canonical,
        "revision": snapshot.revision,
        "revision_ref": snapshot.revision_ref,
        "base_model": base_locator,
    }
    result["subset"] = subset
    if args.json:
        print(json.dumps(result, indent=2, ensure_ascii=False))
    else:
        print_classification(result)
    return 0


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
            "(metadata + bounded header reads, no download) ..."
        )
    failed = 0
    digests = []
    # Classification reads across a refresh are budgeted as a whole,
    # query_limit-style; rows past the budget price the full repo.
    spent = {"requests": 0, "bytes": 0}
    budget_noted = False
    closed_rows = 0
    for index, entry in enumerate(selected, 1):
        parsed = try_parse_source(entry["source"])
        if parsed is None:
            # A closed work: no source to price, nothing to refresh.
            closed_rows += 1
            continue
        over_budget = (
            spent["requests"] >= REFRESH_READ_BUDGET_REQUESTS
            or spent["bytes"] >= REFRESH_READ_BUDGET_BYTES
        )
        if over_budget and not budget_noted and not quiet:
            print(
                f"read budget reached ({REFRESH_READ_BUDGET_REQUESTS} requests / "
                f"{REFRESH_READ_BUDGET_BYTES // 1024**2} MiB) — remaining rows "
                "priced as the full repo",
                file=sys.stderr,
            )
            budget_noted = True
        extra = f"  [{', '.join(entry['include'])}]" if entry.get("include") else ""
        if not quiet:
            # Announce before the work: a big row costs seconds of Hub
            # metadata and header reads, and silence reads as a hang.
            print(
                f"  [{index}/{len(selected)}] {entry['source']}{extra} ...",
                end="",
                flush=True,
            )
        tick = None
        if not quiet:

            def tick(*_args):
                print(".", end="", flush=True)

        try:
            est = estimate(
                entry["source"],
                revision=entry.get("revision"),
                vault=vault,
                include=entry.get("include"),
                full=over_budget,
                progress=lambda *a, **k: None,
                on_read=tick,
            )
        except SystemExit as exc:
            if not quiet:
                print(" failed", flush=True)
            print(f"warning: {warning_detail(exc)}", file=sys.stderr)
            failed += 1
            continue
        read = ((est.get("subset") or {}).get("classification") or {}).get("read")
        if read:
            spent["requests"] += read.get("requests") or 0
            spent["bytes"] += read.get("bytes_fetched") or 0
        digest = estimate_digest(est)
        adopt_resolved_source(catalog, entry, est["source"]["address"])
        entry["estimate"] = digest
        digests.append(
            {"source": entry["source"], "include": entry.get("include"), **digest}
        )
        if quiet:
            continue
        hints = f"  {', '.join(digest['hints'])}" if digest.get("hints") else ""
        params = ""
        if digest.get("parameters"):
            dtype = (
                f" {digest['dominant_dtype']}" if digest.get("dominant_dtype") else ""
            )
            params = f"  {human_params(digest['parameters'])}{dtype}"
        print(
            f" {human_size(digest['payload_bytes'])}  "
            f"{digest.get('license') or '?'}{hints}{params}",
            flush=True,
        )
    dry_run = bool(getattr(args, "dry_run", False))
    if not dry_run:
        catalog["updated"] = utc_now()
        save_catalog(cat_path, catalog)
        write_catalog_readme(cat_path.parent, catalog)
    if args.json:
        print(json.dumps(digests, indent=2, ensure_ascii=False))
        return 1 if failed else 0
    records = bundle_records(vault)
    stats = overlay_stats(overlay(catalog, records))
    print(f"{'Would update' if dry_run else 'Updated'} {cat_path}")
    if closed_rows:
        print(
            f"  {closed_rows} closed row{'s hold their' if closed_rows != 1 else ' holds its'} "
            "place (no source to price)"
        )
    unknown = " + ?" if stats["remaining_unknown"] else ""
    print(f"  remaining (this vault): {human_size(stats['remaining_bytes'])}{unknown}")
    if dry_run:
        _dry_run_done(args, "catalog not written", "refresh")
    return 1 if failed else 0


def _tty_confirm(question: str, *, default: bool = True) -> bool:
    """Ask on the terminal; ``y`` means yes, ``n`` or EOF mean no.

    Enter means ``default``: yes for a question that only pauses work, no
    for one that removes bytes. The archive's SIGINT handler swallows a
    first Ctrl-C (it requests a clean stop mid-transfer), which at a prompt
    would just leave the user waiting; the default handler is restored for
    the question so Ctrl-C aborts it.
    """
    import signal

    previous = signal.signal(signal.SIGINT, signal.default_int_handler)
    try:
        answer = input(question)
    except EOFError:
        return False
    finally:
        signal.signal(signal.SIGINT, previous)
    answer = answer.strip().lower()
    return default if answer == "" else answer in {"y", "yes"}


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
    if getattr(args, "board", None) and not getattr(args, "_board_progress", None):
        _claim_on_board(args, vault, source, revision, include)
    max_bytes = (
        int(args.max_gb * 1024**3) if args.max_gb is not None else args.max_bytes
    )
    # Ask before a transfer that cannot finish only when someone is there
    # to answer; cron and pipes proceed, as they always have.
    confirm = None if args.yes or not _on_a_terminal() else _tty_confirm
    choose = None
    if (
        _on_a_terminal()
        and os.environ.get("TERM", "dumb") not in {"", "dumb"}
        and not args.yes
        and not args.full
        and not include
        and not args.shard
        and not getattr(args, "board", None)
        and getattr(args, "next", None) is None
    ):
        from .collection_tui import choose_collection

        choose = choose_collection
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
            full=args.full,
            confirm=confirm,
            choose=choose,
            resume_scope=not getattr(args, "board", None)
            and getattr(args, "next", None) is None,
        )
    except PartialTransfer as stop:
        _push_board_progress(args, state="paused", bundle_dir=stop.bundle_dir)
        print(f"\nArchive paused cleanly ({stop.reason}: {stop.detail}).")
        print(f"Partial bundle: {stop.bundle_dir}")
        if stop.reason == "handed_off":
            print(
                "Everything here is verified or already handed off to another vault. "
                "Assemble the halves into one vault, then run archive there to register:"
            )
            print(
                f"  darsay --vault <vault-with-the-other-half> assemble {stop.bundle_dir} --handoff"
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
        _push_board_progress(args, state="paused")
        print("\nInterrupted.", file=sys.stderr)
        return 130
    except SystemExit:
        # The archive refused before anything durable started (gated repo,
        # bad revision, full disk refusal): hand a claimed board row back
        # rather than leave the claim dangling until it goes stale.
        ctx = getattr(args, "_board_progress", None)
        if ctx:
            from contextlib import suppress

            from .board import release

            with suppress(SystemExit, OSError):
                release(ctx["board"], ctx["entry_id"], ctx["client"])
        raise
    if bundle is None:  # --dry-run printed the plan and intentionally did not register
        ctx = getattr(args, "_board_progress", None)
        if ctx:  # a dry run holds nothing; hand the row back
            from contextlib import suppress

            from .board import release

            with suppress(SystemExit, OSError):
                release(ctx["board"], ctx["entry_id"], ctx["client"])
        _dry_run_done(args, "no payload bytes moved", "archive")
        return 0
    _push_board_progress(args, state="done", percent=100)
    bundle_id = f"{bundle.parent.name}@{bundle.name}"
    from .archiver import load_manifest

    artifact = load_manifest(bundle).get("artifact_type")
    next_cmd = _bundle_command(
        args, "info" if artifact == "dataset" else "run", bundle, bundle_id
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
    if getattr(args, "board", None):
        raise SystemExit(
            "error: --next picks the board's row itself; drop --board "
            "(or drop --next to choose the source yourself)"
        )
    if next_flag != "" and source:
        raise SystemExit(
            "error: --next already chose the catalog; do not also pass SOURCE"
        )
    catalog_spec = source if next_flag == "" else next_flag
    if not catalog_spec:
        raise SystemExit("error: --next requires a catalog")
    board_hit = _board_target(catalog_spec)
    if board_hit is not None:
        return _archive_next_from_board(args, vault, board_hit)
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


def _claim_on_board(args, vault, source, revision, include) -> None:
    """``archive SOURCE --board URL``: claim the matching row before fetching.

    The chosen source, not the board's priority, decides what is fetched;
    the board still gets the claim and the progress gauge. A source with
    no matching row archives unclaimed, with a warning — archiving must
    never silently edit a board. Naming the source is the deliberate act,
    so the claim carries ``refetch``: it goes through even on a row the
    board already checks off as have (``--next`` never does that).
    """
    from .board import claim, client_id, entry_for, fetch_entries, parse_board_url

    board = parse_board_url(args.board)
    if board is None:
        raise SystemExit(
            f"error: --board expects a board URL, got {args.board!r}\n"
            "  hint: https://darsay.io/b/<board-id>"
        )
    entries = fetch_entries(board)
    entry = entry_for(entries, source, revision, include)
    entry_id = entry.get("id") if entry else None
    if not isinstance(entry_id, int):
        print(
            f"WARNING: {source} is not a row on {board.page_url} — "
            "archiving without a claim",
            file=sys.stderr,
        )
        return
    if entry.get("status") == "have":
        print(
            f"NOTE: {source} is already checked off as have on "
            f"{board.page_url} — re-fetching deliberately.",
            file=sys.stderr,
        )
    client = client_id(vault)
    ok, other = claim(board, entry_id, client, refetch=True)
    if not ok:
        holder = other.get("client") or "another client"
        pct = f" ({other.get('percent')}%)" if other.get("percent") is not None else ""
        raise SystemExit(
            f"error: {source} is claimed by {holder}{pct} on {board.page_url}\n"
            "  A claim expires 24h after its last progress report; release "
            "it from the board page if it is stale."
        )
    args._board_progress = {"board": board, "entry_id": entry_id, "client": client}
    print(f"Claimed {source} on {board.page_url} as {client}")


def _archive_next_from_board(args, vault, board_hit):
    """Pick the board's next unfinished row and claim it before fetching.

    Unfinished is the board's judgment, not just the local overlay's: a
    row the board already marks ``have`` — a client reported done, or a
    human checked it off — is never picked, even though board status
    never enters catalog.json and this vault alone would still want it.
    A row another client holds a live claim on is skipped — that is the
    coordination boards exist for. The claim context rides on ``args``;
    archive boundaries report progress through it, and the board renders
    the gauge.
    """
    from .board import claim, client_id, entry_for, fetch_entries
    from .catalog import (
        filter_want,
        load_catalog,
        next_idle_message,
        overlay,
        realize_from_overlay,
        sort_rows,
    )
    from .vault import bundle_records

    board, cat_path = board_hit
    catalog = load_catalog(cat_path)
    rows = overlay(catalog, bundle_records(vault))
    candidates = sort_rows(filter_want(rows), "next")
    if not candidates:
        _finish_next(*next_idle_message(catalog, rows))
        return None
    entries = fetch_entries(board)
    client = client_id(vault)
    checked_off = held = 0
    for row in candidates:
        entry = entry_for(
            entries, row["source"], row.get("revision"), row.get("include")
        )
        entry_id = entry.get("id") if entry else None
        if not isinstance(entry_id, int):
            continue
        if entry.get("status") == "have":
            checked_off += 1
            continue
        ok, other = claim(board, entry_id, client, percent=int(row.get("percent") or 0))
        if not ok:
            holder = other.get("client") or "another client"
            pct = (
                f" ({other.get('percent')}%)"
                if other.get("percent") is not None
                else ""
            )
            print(f"Skipping {row['source']} — claimed by {holder}{pct}")
            held += 1
            continue
        args._board_progress = {"board": board, "entry_id": entry_id, "client": client}
        source, revision, include = realize_from_overlay(row)
        state = "partial" if row.get("status") == "partial" else "want"
        extra = f"  include={','.join(include)}" if include else ""
        print(
            f"Next from board {board.page_url} "
            f"(desire {row.get('desire') or '—'}, {state}): {source}{extra}"
            f"  [claimed as {client}]"
        )
        return source, revision, include
    reasons = []
    if checked_off:
        reasons.append(f"{checked_off} checked off as have on the board")
    if held:
        reasons.append(f"{held} claimed by another client")
    detail = ", ".join(reasons) or "no unfinished row matches a board row"
    print(f"Nothing to fetch from {board.page_url}: {detail}.", file=sys.stderr)
    return None


def cmd_verify(args) -> int:
    from .verify import verify_bundle

    bundles = _resolve_bundles(args, require_manifest=True, verb="verify")
    if not bundles:
        print("No registered bundles to verify.")
        return 0
    failed = []
    for i, bundle in enumerate(bundles):
        if i:
            print()
        report = verify_bundle(bundle)
        if report["result"] != "pass":
            failed.append(bundle)
    if len(bundles) > 1:
        print()
        if failed:
            print(f"Verified {len(bundles)} bundles: {len(failed)} FAILED.")
            for bundle in failed:
                print(f"  fail  {bundle}")
        else:
            print(f"Verified {len(bundles)} bundles: all pass.")
    return 1 if failed else 0


def cmd_smoke(args) -> int:
    from .smoke import run_smoke

    results = run_smoke(_bundle_dir(args), inference=args.inference)
    failed = any(r.get("status") == "fail" for r in results.values())
    print(json.dumps(results, indent=2))
    return 1 if failed else 0


def cmd_list(args) -> int:
    from .vault import bundle_records, using_implicit_vault, vault_absence

    machine = args.json or args.ids or getattr(args, "next", False)
    vault = _vault_path(args, announce=not machine)
    # A default vault that does not exist yet is a first run, not an error;
    # an explicitly named one that is missing (an unmounted disk) is.
    absent = vault_absence(vault)
    if absent and not using_implicit_vault(args.vault):
        print(f"error: {absent}: {vault}", file=sys.stderr)
        return 1
    records = bundle_records(vault)
    catalog_spec = getattr(args, "catalog", None)
    if catalog_spec:
        # A plain directory that is not a catalog is another vault — a drive:
        # show what it holds against this one. A catalog dir (holds
        # catalog.json) and a .json file stay catalog overlays.
        other = Path(catalog_spec).expanduser()
        if other.is_dir() and not (other / "catalog.json").is_file():
            return _list_vault_overlay(args, vault, other, records)
        board_hit = _board_target(catalog_spec)
        if board_hit is not None:
            catalog_spec = str(board_hit[1])
        return _list_catalog(args, vault, records, catalog_spec)
    return _list_vault(args, vault, records)


def _bundle_hash_value(path) -> str | None:
    """The recorded bundle hash, read straight from manifest.json (any schema)."""
    try:
        m = json.loads((Path(path) / "manifest.json").read_text(encoding="utf-8"))
        return m["inventory"]["bundle_hash"]["value"]
    except (OSError, json.JSONDecodeError, KeyError, TypeError):
        return None


def _list_vault_overlay(args, vault, other, records) -> int:
    """Show what another vault (a drive) holds, against this one.

    Read-only. Each bundle on the drive is ``new`` (not here), ``have``
    (here, same bundle hash), ``differ`` (here, but the bytes differ), or
    ``partial`` (an in-progress archive on the drive). The order is what an
    operator copies first: new, then differ, then partial, then have.
    """
    from .vault import bundle_records, vault_absence

    absent = vault_absence(other)
    if absent:
        print(f"error: {absent}: {other}", file=sys.stderr)
        return 1
    here = {r["bundle_id"]: r for r in records if not r.get("partial")}
    rows = []
    for r in bundle_records(other):
        bid = r["bundle_id"]
        if r.get("partial"):
            status = "partial"
        elif bid not in here:
            status = "new"
        else:
            there_hash = _bundle_hash_value(r["path"])
            here_hash = _bundle_hash_value(here[bid]["path"])
            status = (
                "differ"
                if there_hash and here_hash and there_hash != here_hash
                else "have"
            )
        rows.append(
            {
                "bundle_id": bid,
                "status": status,
                "path": r["path"],
                "size": r.get("size"),
                "payload_bytes": r.get("payload_bytes"),
            }
        )
    if args.json:
        print(
            json.dumps(
                {"vault": str(vault), "drive": str(other), "bundles": rows},
                indent=2,
                ensure_ascii=False,
            )
        )
        return 0
    order = {"new": 0, "differ": 1, "partial": 2, "have": 3}
    rows.sort(key=lambda row: (order.get(row["status"], 9), row["bundle_id"]))
    if args.ids:
        # The actionable set: what the drive has that this vault does not (or
        # holds differently). A plain inventory of the drive is `list --ids`
        # run against it directly.
        for row in rows:
            if row["status"] != "have":
                print(row["bundle_id"])
        return 0
    if not rows:
        print(f"No bundles on the drive at {other}/")
        return 0
    counts = {}
    for row in rows:
        counts[row["status"]] = counts.get(row["status"], 0) + 1
    parts = [
        f"{counts[s]} {label}"
        for s, label in (
            ("new", "new"),
            ("differ", "differ"),
            ("partial", "partial"),
            ("have", "already here"),
        )
        if counts.get(s)
    ]
    print(f"\nDrive {other}  vs vault {vault}")
    print(f"  {' · '.join(parts)}  (of {len(rows)} on the drive)\n")
    width = max((len(row["bundle_id"]) for row in rows), default=6)
    print(f"{'STATUS':<8}{'BUNDLE':<{width + 2}}SIZE")
    for row in rows:
        print(f"{row['status']:<8}{row['bundle_id']:<{width + 2}}{row['size'] or '?'}")
    _print_loose_note(other)
    return 0


def _print_loose_note(vault) -> None:
    """After a listing, name any bundle on disk that is not in the layout."""
    from .vault import find_loose_bundles

    loose = find_loose_bundles(Path(vault))
    if not loose:
        return
    n = len(loose)
    print(
        f"\nnote: {n} bundle{'s' if n != 1 else ''} on disk "
        f"{'is' if n == 1 else 'are'} not in the name/revision layout list reads:"
    )
    for d in loose:
        print(f"  {d}")
    print(f"  place {'it' if n == 1 else 'them'} with: darsay cp <path> {vault}")


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
        _print_loose_note(vault)
        return 0
    if not rows:
        print("nothing in progress in the vault")
        return 0
    rows = sort_rows(rows, sort or "name")
    stats = overlay_stats(rows)
    print_catalog_table(rows, header_line=vault_header_line(vault, stats))
    _print_loose_note(vault)
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
        printable = [r for r in rows if r.get("status") != "closed"]
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
        dry_run=args.dry_run,
    )
    dest = Path(catalog["_path"]).parent
    print(f"{'Would create catalog' if args.dry_run else 'Catalog ready'}: {dest}")
    print(f"  id:       {catalog['id']}")
    print(f"  catalog:  {dest / 'catalog.json'}")
    print(f"  readme:   {dest / 'README.md'}")
    print(
        f"  curation: {dest / 'curation.md'}  <- edit this, then `darsay catalog regen`"
    )
    if args.dry_run:
        _dry_run_done(args, "nothing written", "create")
        return 0
    print(
        f"  next:     darsay catalog add {catalog['id']} huggingface:owner/name --desire 8"
    )
    return 0


def cmd_catalog_add(args) -> int:
    from .catalog import (
        estimate_digest,
        is_home,
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
    board_hit = _board_target(args.catalog)
    if board_hit is not None:
        board, path = board_hit
    else:
        board, path = None, resolve_catalog(vault, args.catalog)
    require_writable(vault, path, bool(args.write) or board is not None)
    catalog = load_catalog(path)
    source = args.source
    digest = None
    extra = ""
    closed = is_home(source)
    if closed and getattr(args, "estimate", False):
        print(
            "note: a closed work has no source to price; --estimate ignored",
            file=sys.stderr,
        )
    if getattr(args, "estimate", False) and not closed:
        est = estimate(
            source,
            revision=args.revision,
            vault=vault,
            include=args.include,
            progress=print,
        )
        source = est["source"]["address"]
        digest = estimate_digest(est)
        hints = f"  {', '.join(digest['hints'])}" if digest.get("hints") else ""
        extra = f"  {human_size(digest['payload_bytes'])}{hints}  (as of {digest['as_of'][:10]})"
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
    if not args.dry_run:
        save_catalog(path, catalog)
        write_catalog_readme(path.parent, catalog)
    inc = f"  include={','.join(entry['include'])}" if entry.get("include") else ""
    desire = f"  desire={entry['desire']}" if entry.get("desire") is not None else ""
    if action == "added":
        verb = "Would add" if args.dry_run else "Added"
        if closed:
            hint = "  closed — a home page, not a source: holds the work's place in its family"
        else:
            hint = extra or "  (no estimate yet; darsay estimate " + catalog["id"] + ")"
        print(f"{verb} {entry['source']}{inc}{desire}{hint}")
        if closed:
            print(
                "  when weights are published: darsay catalog add "
                f"{catalog['id']} <source-ref>, then drop this row"
            )
    else:
        verb = "Would update" if args.dry_run else "Updated"
        print(f"{verb} {entry['source']}{inc}{desire}{extra}")
    if args.dry_run:
        _dry_run_done(args, "nothing written", "add" if action == "added" else "update")
    elif board is not None:
        from .board import push_catalog

        push_catalog(board, path)
        print(f"Pushed to {board.page_url}")
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
    board_hit = _board_target(args.catalog)
    if board_hit is not None:
        board, path = board_hit
    else:
        board, path = None, resolve_catalog(vault, args.catalog)
    require_writable(vault, path, bool(args.write) or board is not None)
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
    if args.dry_run:
        print(f"Would drop {removed['source']} from {catalog['id']}")
        _dry_run_done(args, "nothing written", "drop")
        return 0
    save_catalog(path, catalog)
    write_catalog_readme(path.parent, catalog)
    print(f"Dropped {removed['source']} from {catalog['id']}")
    if board is not None:
        from .board import push_catalog

        push_catalog(board, path)
        print(f"Pushed to {board.page_url}")
    return 0


def cmd_catalog_adopt(args) -> int:
    from .catalog import (
        adopt_entries,
        adoptable_entries,
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
    other_hit = _board_target(args.other)
    src_path = (
        other_hit[1] if other_hit is not None else resolve_catalog(vault, args.other)
    )
    dest = load_catalog(dest_path)
    other = load_catalog(src_path)
    new_entries, skipped = adoptable_entries(dest, other)
    if not args.dry_run:
        adopt_entries(dest, other)
        save_catalog(dest_path, dest)
        write_catalog_readme(dest_path.parent, dest)
    n = len(new_entries)
    print(
        f"{'Would adopt' if args.dry_run else 'Adopted'} {n} "
        f"entr{'y' if n == 1 else 'ies'} from {other['id']} → {dest['id']} "
        f"({skipped} already present)"
    )
    for entry in new_entries:
        print(f"  {_entry_label(entry)}")
    if args.dry_run:
        _dry_run_done(args, "nothing written", "adopt")
        return 0
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
    readme = path.parent / "README.md"
    if args.dry_run:
        added, removed = write_catalog_readme(path.parent, catalog, dry_run=True)
        print(f"Would regenerate {readme}{_delta_note(added, removed)}")
        _dry_run_done(args, "nothing written", "regenerate")
        return 0
    catalog["updated"] = utc_now()
    save_catalog(path, catalog)
    added, removed = write_catalog_readme(path.parent, catalog)
    print(f"Regenerated {readme}{_delta_note(added, removed)}")
    return 0


def cmd_rm(args) -> int:
    import shutil

    from .readme_gen import human_size
    from .vault import dir_size, prune_empty_parent

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
    sizes = [dir_size(bundle) for bundle in unique]

    def listing(heading: str) -> None:
        print(heading)
        for bundle, size in zip(unique, sizes, strict=True):
            print(f"  {human_size(size):>10}  {bundle}")
        if len(unique) > 1:
            print(f"  {human_size(sum(sizes)):>10}  total ({len(unique)} bundles)")

    if args.dry_run:
        listing("Would remove:")
        _dry_run_done(args, "nothing removed", "remove")
        return 0
    if not args.yes:
        listing("Will remove:")
        try:
            answer = input("Type yes to confirm: ")
        except EOFError:
            answer = ""
        if answer.strip().lower() != "yes":
            print("Aborted.")
            return 1
    for bundle in unique:
        shutil.rmtree(bundle)
        prune_empty_parent(bundle)
        print(f"Removed {bundle}")
    return 0


def cmd_du(args) -> int:
    from .readme_gen import human_size
    from .vault import bundle_records, dir_size, using_implicit_vault, vault_absence

    vault = _vault_path(args, announce=not args.json)
    absent = vault_absence(vault)
    if absent and not using_implicit_vault(args.vault):
        print(f"error: {absent}: {vault}", file=sys.stderr)
        return 1
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
    if args.assignments:
        from .config import write_vault_settings

        assignments = {}
        for text in args.assignments:
            name, sep, value = text.partition("=")
            if not sep or not name.strip():
                raise SystemExit(f"error: expected KEY=VALUE, got {text!r}")
            assignments[name.strip()] = value.strip()
        if args.dry_run:
            for name, value in assignments.items():
                print(f'Would set {name} = "{value}" in {files["vault"]}')
            _dry_run_done(args, "nothing written", "set")
            return 0
        path = write_vault_settings(vault, assignments)
        for name, value in assignments.items():
            print(f'Set {name} = "{value}" in {path}')
        return 0
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
            print(
                "  hydration:  not hydrated "
                f"({_bundle_command(args, 'hydrate', bundle, m['bundle_id'])})"
            )
    return 0


def cmd_export(args) -> int:
    from .export import export_bundle

    out = export_bundle(_bundle_dir(args), Path(args.output_dir), dry_run=args.dry_run)
    if args.dry_run:
        _dry_run_done(args, "nothing written", "export")
        return 0
    print(f"Export ready: {out}")
    return 0


def cmd_import(args) -> int:
    from .export import import_bundle

    import_bundle(
        Path(args.file),
        _vault_path(args, announce=True),
        force=args.force,
        dry_run=args.dry_run,
    )
    if args.dry_run:
        _dry_run_done(args, "nothing unpacked", "import")
    return 0


def cmd_migrate(args) -> int:
    from .migrate import migrate_bundle, migration_plan, vault_migration_plans
    from .schema import MANIFEST_SCHEMA_MAJOR

    specs = list(args.bundles or [])
    if args.all and specs:
        raise SystemExit("error: name bundles or pass --all, not both")
    if not args.all and not specs:
        raise SystemExit(
            "error: migrate needs a bundle (path, id, or unique prefix), or --all "
            "for every record in the vault that predates this darsay\n"
            "  hint: darsay list   (rows marked `migrate` are the ones)"
        )
    machine = args.json
    quiet = lambda *a, **k: None  # noqa: E731
    progress = quiet if machine else print
    if args.all:
        vault = _vault_path(args, announce=not machine)
        plans = vault_migration_plans(vault)
    else:
        plans = [
            migration_plan(_bundle_dir(args, spec, require_manifest=False))
            for spec in specs
        ]
    todo = [plan for plan in plans if plan["status"] == "migrate"]
    current = [plan for plan in plans if plan["status"] == "current"]

    if args.all and not machine:
        if not todo:
            print(
                f"Every record in {vault} is {MANIFEST_SCHEMA_MAJOR}.x "
                f"({len(current)} bundle{'s' if len(current) != 1 else ''}) — "
                "nothing to migrate"
            )
            return 0
        print(
            f"{len(todo)} of {len(plans)} record{'s' if len(plans) != 1 else ''} in "
            f"{vault} predate{'s' if len(todo) == 1 else ''} this darsay "
            f"({MANIFEST_SCHEMA_MAJOR}.x); {len(current)} "
            f"{'is' if len(current) == 1 else 'are'} current"
        )
    # A named bundle that is already current says so; under --all the
    # header counts the current ones and only the work is shown.
    for i, plan in enumerate(todo if args.all else plans):
        if i and not machine:
            print()
        migrate_bundle(
            Path(plan["path"]), progress=progress, dry_run=args.dry_run, plan=plan
        )

    if machine:
        print(
            json.dumps(
                {
                    "schema": {
                        "reads": f"{MANIFEST_SCHEMA_MAJOR}.x",
                        "writes": plans[0]["to_schema"] if plans else None,
                    },
                    "dry_run": bool(args.dry_run),
                    "bundles": [
                        {k: v for k, v in plan.items() if k != "record"}
                        for plan in plans
                    ],
                },
                indent=2,
                ensure_ascii=False,
            )
        )
        return 0
    if not todo:
        return 0
    if args.dry_run:
        print()
        _dry_run_done(args, "nothing written", "migrate")
        return 0
    # A record whose last pass was at this path on this host still stands;
    # one that arrived from elsewhere is hashed where it landed.
    pending = [plan for plan in todo if not plan.get("verified_here")]
    stands = [plan for plan in todo if plan.get("verified_here")]
    print()
    # The id names a bundle the vault lists (with --vault when it is not the
    # default); one migrated by a path elsewhere is named by that path.
    cmd = lambda plan: _bundle_command(  # noqa: E731
        args, "verify", Path(plan["path"]), plan["bundle_id"]
    )
    if len(todo) == 1:
        plan = todo[0]
        if pending:
            print(f"  next:  {cmd(plan)}   (re-hash the payload where it landed)")
        else:
            print(
                f"  done:  the record says the payload passed verification at this "
                f"path on {plan['verified_here']}; `{cmd(plan)}` re-hashes it at "
                "any time"
            )
        return 0
    print(f"Migrated {len(todo)} records to schema {todo[0]['to_schema']}.")
    if pending:
        print("  next: re-hash each payload where it landed:")
        for plan in pending:
            print(f"  {cmd(plan)}")
    if stands:
        print(
            f"  {len(stands)} record{'s' if len(stands) != 1 else ''} say the "
            "payload passed verification at its path; `darsay verify` re-hashes "
            "at any time"
        )
    return 0


def cmd_mv(args) -> int:
    from .relocate import move_bundle

    dest = Path(args.dest_vault).expanduser()
    # Resolve partials too, so a partial gets the verb that fits it instead
    # of "no manifest.json"; move_plan does the refusing.
    bundles = _resolve_bundles(args, require_manifest=False, verb="move")
    if not bundles:
        print("No registered bundles to move.")
        return 0
    for i, bundle in enumerate(bundles):
        if i:
            print()
        move_bundle(bundle, dest, dry_run=args.dry_run)
    if args.dry_run:
        _dry_run_done(args, "nothing copied, nothing removed", "move")
    return 0


def cmd_cp(args) -> int:
    from .relocate import copy_bundle

    dest = Path(args.dest_vault).expanduser()
    bundles = _resolve_bundles(args, require_manifest=False, verb="copy")
    if not bundles:
        print("No registered bundles to copy.")
        return 0
    for i, bundle in enumerate(bundles):
        if i:
            print()
        copy_bundle(bundle, dest, dry_run=args.dry_run)
    if args.dry_run:
        _dry_run_done(args, "nothing copied", "copy")
    return 0


def cmd_assemble(args) -> int:
    from .transfer import assemble_partials

    if args.dry_run:
        from .transfer import assemble_plan, print_assemble_plan

        plan = assemble_plan(
            [Path(path) for path in args.partials],
            _vault_path(args, announce=True),
            handoff=args.handoff,
            rehash=args.rehash,
        )
        print_assemble_plan(plan)
        skipped = (
            "nothing copied, nothing released" if args.handoff else "nothing copied"
        )
        _dry_run_done(args, skipped, "assemble")
        return 0
    bundle, plan = assemble_partials(
        [Path(path) for path in args.partials],
        _vault_path(args, announce=True),
        handoff=args.handoff,
        rehash=args.rehash,
    )
    if (bundle / "manifest.json").is_file():
        print(f"\nDestination is already a registered bundle: {bundle}")
        if args.handoff:
            print(
                "Source files dest already holds as verified were released "
                "(the source is a skeleton, or was removed if nothing remained)."
            )
        return 0
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
        _dry_run_done(args, "nothing built", "hydrate")
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
        dry_run=args.dry_run,
    )
    if record.get("dry_run"):
        _dry_run_done(args, "nothing run", "run")
        return 0
    return 0 if record["status"] == "pass" else 1


def cmd_dehydrate(args) -> int:
    from .hydrate import dehydrate_bundle

    hydrated = dehydrate_bundle(_bundle_dir(args), dry_run=args.dry_run)
    if args.dry_run and hydrated:
        _dry_run_done(args, "nothing removed", "dehydrate")
    return 0


def cmd_envs(args) -> int:
    from .hydrate import list_envs, prune_envs
    from .readme_gen import human_size

    vault = _vault_path(args)
    if args.prune:
        freed = prune_envs(vault, dry_run=args.dry_run)
        if args.dry_run and freed:
            _dry_run_done(args, "nothing removed", "prune")
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
    from .hashing import SHA256SUMS_NAME, write_sha256sums
    from .readme_gen import write_bundle_readme

    bundle = _bundle_dir(args)
    manifest = load_manifest(bundle)
    readme = bundle / "README.md"
    added, removed = write_bundle_readme(bundle, manifest, dry_run=args.dry_run)
    if args.dry_run:
        print(f"Would regenerate {readme}{_delta_note(added, removed)}")
        print(f"Would regenerate {bundle / SHA256SUMS_NAME}")
        _dry_run_done(args, "nothing written", "regenerate")
        return 0
    print(f"Regenerated {readme}{_delta_note(added, removed)}")
    print(f"Regenerated {write_sha256sums(bundle, manifest)}")
    return 0


def cmd_doctor(args) -> int:
    """Run the offline, reversible vault doctor without masking its exit contract."""
    from .doctor import DoctorError, run

    try:
        return run(args, _vault_path(args))
    except DoctorError as exc:
        if getattr(args, "json", False) or getattr(args, "robot_triage", False):
            print(
                json.dumps(
                    {
                        "schema_version": 1,
                        "status": "error",
                        "exit_code": exc.code,
                        "error": {"code": exc.code, "message": str(exc)},
                    },
                    indent=2,
                    sort_keys=True,
                )
            )
        elif not getattr(args, "quiet", False):
            print(f"darsay doctor: {exc}", file=sys.stderr)
        return exc.code


def _add_doctor_output_flags(parser, *, suppress_defaults: bool = False) -> None:
    default = argparse.SUPPRESS if suppress_defaults else False
    parser.add_argument(
        "--json",
        action="store_true",
        default=default,
        help="stable machine-readable output",
    )
    parser.add_argument(
        "--quiet", action="store_true", default=default, help="suppress normal output"
    )
    parser.add_argument(
        "--verbose", action="store_true", default=default, help="show evidence details"
    )
    parser.add_argument(
        "--no-color",
        action="store_true",
        default=default,
        help="disable color (doctor output is currently plain text)",
    )
    parser.add_argument(
        "--no-progress",
        action="store_true",
        default=default,
        help="disable progress output (doctor is currently non-interactive)",
    )


def _add_doctor_diagnose_flags(parser, *, suppress_defaults: bool = False) -> None:
    default = argparse.SUPPRESS if suppress_defaults else False
    append_default = argparse.SUPPRESS if suppress_defaults else None
    parser.add_argument(
        "-n",
        "--dry-run",
        action="store_true",
        default=default,
        help="with --fix, show proposed actions without changing vault state",
    )
    parser.add_argument(
        "--only",
        action="append",
        default=append_default,
        metavar="CHECK",
        help="run/report only this check or finding id (repeatable or comma-separated)",
    )
    parser.add_argument(
        "--skip",
        action="append",
        default=append_default,
        metavar="CHECK",
        help="skip this check or finding id (repeatable or comma-separated)",
    )
    parser.add_argument(
        "--since", default=argparse.SUPPRESS if suppress_defaults else None
    )
    parser.add_argument(
        "--online",
        action="store_true",
        default=default,
        help="request online checks (none are currently supported)",
    )
    parser.add_argument(
        "--explain",
        action="store_true",
        default=default,
        help="include evidence details in human output",
    )
    parser.add_argument(
        "--severity",
        choices=("info", "warning", "error", "critical"),
        default=argparse.SUPPRESS if suppress_defaults else None,
        help="minimum reported severity",
    )
    parser.add_argument(
        "--budget",
        type=_positive_float,
        default=argparse.SUPPRESS if suppress_defaults else None,
        metavar="SECONDS",
        help="stop the initial scan after this many seconds",
    )
    parser.add_argument(
        "--quick",
        action="store_true",
        default=default,
        help="skip payload hashes and generated README comparison",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        default=default,
        help="reserved for explicitly forceable future fixers",
    )
    parser.add_argument(
        "-y",
        "--yes",
        action="store_true",
        default=default,
        help="accept prompts (current low-risk fixers do not prompt)",
    )
    parser.add_argument(
        "--robot-triage",
        action="store_true",
        default=default,
        help="emit one machine-readable diagnosis and repair plan; never fix",
    )


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
    p.add_argument(
        "--full",
        action="store_true",
        help="price the whole repo, including exact duplicate weight files",
    )
    p.add_argument("--json", action="store_true", help="machine-readable output")
    p.add_argument(
        "--write",
        action="store_true",
        help="allow writing a path-addressed catalog (vault-named catalogs are always writable)",
    )
    _add_dry_run(p, "with a catalog, print the refreshed sizes; save nothing")
    p.set_defaults(func=cmd_estimate)

    p = add_cmd(
        "classify",
        help="preservation evidence and retention for model weights (bounded header reads)",
    )
    p.add_argument(
        "source", help="e.g. huggingface:Qwen/Qwen3-0.6B or a huggingface.co URL"
    )
    p.add_argument("--revision", help="branch, tag, or commit (default: main)")
    p.add_argument(
        "--include",
        action="append",
        metavar="GLOB",
        help="classify only payload files matching GLOB (repeatable)",
    )
    p.add_argument("--json", action="store_true", help="machine-readable output")
    p.set_defaults(func=cmd_classify)

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
        "--full",
        action="store_true",
        help="fetch the whole repo, including exact duplicate weight files",
    )
    p.add_argument(
        "--board",
        metavar="URL",
        help="claim SOURCE's row on this darsay.io board and report progress "
        "(--next picks the board's row for you; --board lets you pick)",
    )
    _add_dry_run(
        p, "pin, reconcile, and print the transfer plan; move no payload bytes"
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
        help="skip the collection picker (use the default archive policy) and "
        "disk-preflight confirmation; non-interactive runs never prompt",
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
        help=(
            "parallel transfer streams: N small files at once, and N large "
            "files at once with one hash thread verifying alongside "
            "(default: 4)"
        ),
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

    p = add_cmd("verify", help="re-hash bundles and compare against their manifests")
    p.add_argument("bundles", nargs="*", metavar="BUNDLE", help=bundle_help)
    p.add_argument(
        "--all",
        action="store_true",
        help="verify every registered bundle in the vault",
    )
    p.set_defaults(func=cmd_verify)

    p = add_cmd("smoke", help="run smoke tests on a bundle")
    p.add_argument("bundle", help=bundle_help)
    p.add_argument(
        "--inference",
        action="store_true",
        help="also load the model and generate (needs torch)",
    )
    p.set_defaults(func=cmd_smoke)

    p = add_cmd(
        "list", help="list bundles, or overlay a catalog or another vault on this one"
    )
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
        choices=("next", "desire", "size", "name", "status", "family"),
        help="row order (default: next when a catalog is given, name for the vault); "
        "family reads the tree: family, generation, size",
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
    _add_dry_run(n, "print the files that would be created; write nothing")
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
    _add_dry_run(a, "print the add or update; write nothing")
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
    _add_dry_run(d, "print the entry that would be dropped; write nothing")
    d.set_defaults(func=cmd_catalog_drop)

    o = add_cat("adopt", help="copy missing entries from another catalog")
    o.add_argument("catalog", help="destination catalog")
    o.add_argument("other", help="source catalog slug or path")
    o.add_argument(
        "--write",
        action="store_true",
        help="allow writing a path-addressed destination",
    )
    _add_dry_run(o, "list the entries that would be adopted; write nothing")
    o.set_defaults(func=cmd_catalog_adopt)

    g = add_cat(
        "regen", help="rebuild a catalog README.md from catalog.json + curation.md"
    )
    g.add_argument("catalog", help="catalog slug or path")
    g.add_argument(
        "--write", action="store_true", help="allow writing a path-addressed catalog"
    )
    _add_dry_run(g, "report whether README.md would change; write nothing")
    g.set_defaults(func=cmd_catalog_regen)

    p = add_cmd("rm", help="delete one or more bundles from the vault")
    p.add_argument("bundles", nargs="+", metavar="BUNDLE", help=bundle_help)
    p.add_argument(
        "-y", "--yes", action="store_true", help="do not prompt for confirmation"
    )
    _add_dry_run(p, "list what would be removed, with sizes; remove nothing")
    p.set_defaults(func=cmd_rm)

    p = add_cmd("du", help="disk usage of bundles and the shared runtime")
    p.add_argument("--json", action="store_true", help="machine-readable totals")
    p.set_defaults(func=cmd_du)

    p = add_cmd(
        "config",
        help=(
            "show effective settings and which config file set them; with "
            "KEY=VALUE, write the vault's config.toml"
        ),
    )
    p.add_argument(
        "assignments",
        nargs="*",
        metavar="KEY=VALUE",
        help=(
            "settings to write to <vault>/config.toml, e.g. host.ssh=root@nas "
            "host.path=/volume1/darsay/vault (the host that owns the vault's disk)"
        ),
    )
    p.add_argument("--json", action="store_true", help="machine-readable settings")
    _add_dry_run(p, "with KEY=VALUE, print what would be written; write nothing")
    p.set_defaults(func=cmd_config)

    p = add_cmd(
        "doctor",
        help="offline vault diagnostics with locked, reversible low-risk repairs",
        description=(
            "Diagnose a darsay vault offline. By default only private evidence is written "
            "under <vault>/.doctor; --fix enables journaled, undoable low-risk repairs."
        ),
    )
    _add_doctor_output_flags(p)
    _add_doctor_diagnose_flags(p)
    p.add_argument(
        "--fix",
        action="store_true",
        help="apply allowlisted low-risk repairs and then diagnose again",
    )
    p.set_defaults(func=cmd_doctor, doctor_command="diagnose")
    doctor_sub = p.add_subparsers(dest="doctor_command", required=False)

    def add_doctor_action(name, **kwargs):
        kwargs.setdefault("parents", [vault_after])
        action = doctor_sub.add_parser(name, **kwargs)
        _add_doctor_output_flags(action, suppress_defaults=True)
        action.set_defaults(func=cmd_doctor)
        return action

    d = add_doctor_action("diagnose", help="run diagnostics (the default action)")
    _add_doctor_diagnose_flags(d, suppress_defaults=True)
    d.add_argument("--fix", action="store_true", default=argparse.SUPPRESS)

    d = add_doctor_action("fix", help="apply low-risk repairs and diagnose again")
    _add_doctor_diagnose_flags(d, suppress_defaults=True)

    d = add_doctor_action("undo", help="reverse one doctor run safely")
    d.add_argument("run_ref", nargs="?", default="latest", help="run id or latest")
    d.add_argument(
        "--strict",
        action="store_true",
        help="refuse any post-repair drift (the default safety policy)",
    )

    d = add_doctor_action("explain", help="explain checks and repair policy")
    d.add_argument("check_id", nargs="?", help="one check id (default: all)")

    add_doctor_action("capabilities", help="describe checks, fixers, scope, and exits")
    add_doctor_action("health", help="fast shallow health probe with no artifacts")
    add_doctor_action("robot-docs", help="print the automation contract")
    add_doctor_action("ls", help="list local doctor runs")

    d = add_doctor_action("diff", help="compare latest findings to a prior run")
    d.add_argument("run_ref", nargs="?", help="prior run id (default: previous)")

    d = add_doctor_action("gc", help="delete old evidence runs, retaining latest")
    d.add_argument("--before", required=True, help="remove runs older than ISO date")
    d.add_argument("-y", "--yes", action="store_true", help="confirm evidence deletion")

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
    _add_dry_run(p, "report whether README.md would change; write nothing")
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
    _add_dry_run(p, "print the plan — engine, env, packages; build nothing")
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
    _add_dry_run(
        p,
        "print what run would do — hydrate first if needed, then the engine, "
        "prompt, and device; run nothing",
    )
    p.set_defaults(func=cmd_run)

    p = add_cmd(
        "dehydrate",
        help="remove a bundle's hydration record (envs are shared; prune via `envs --prune`)",
    )
    p.add_argument("bundle", help=bundle_help)
    _add_dry_run(p, "print the record that would be removed; remove nothing")
    p.set_defaults(func=cmd_dehydrate)

    p = add_cmd("envs", help="list shared runtime envs and which bundles use them")
    p.add_argument(
        "--prune", action="store_true", help="delete envs no hydrated bundle references"
    )
    _add_dry_run(p, "with --prune, list the envs that would be deleted; delete nothing")
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
    _add_dry_run(p, "print what would be packed and where; write nothing")
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
    _add_dry_run(p, "read the marker and print what would land where; unpack nothing")
    p.set_defaults(func=cmd_import)

    p = add_cmd(
        "migrate",
        help=(
            "bring a bundle's record (manifest.json) to the schema this darsay "
            "writes — offline, from the record and the payload; no payload byte "
            "is touched or re-hashed"
        ),
    )
    p.add_argument("bundles", nargs="*", metavar="BUNDLE", help=bundle_help)
    p.add_argument(
        "--all",
        action="store_true",
        help="every registered bundle in the vault whose record predates this darsay",
    )
    p.add_argument(
        "--json", action="store_true", help="machine-readable plan and result"
    )
    _add_dry_run(
        p,
        "print what each record would say after migrating, and where that "
        "comes from; write nothing",
    )
    p.set_defaults(func=cmd_migrate)

    p = add_cmd(
        "mv",
        help=(
            "move a registered bundle into another vault: copy, verify the "
            "copy there, then remove the source (a rename on the same disk; "
            "a copy already there is hashed in place and only what differs "
            "is copied)"
        ),
    )
    p.add_argument("bundles", nargs="*", metavar="BUNDLE", help=bundle_help)
    p.add_argument(
        "dest_vault",
        metavar="VAULT",
        help="destination vault root; must already exist (an unmounted disk is not a vault)",
    )
    p.add_argument(
        "--all",
        action="store_true",
        help="move every registered bundle in the source vault",
    )
    _add_dry_run(
        p,
        "print where each bundle would land and how (rename, copy + verify, or "
        "land on what is already there); move nothing",
    )
    p.set_defaults(func=cmd_mv)

    p = add_cmd(
        "cp",
        help=(
            "copy a registered bundle into another vault: copy, verify the "
            "copy there, keep the source; both manifests record the replica "
            "(run it again to refresh a backup — only what differs is copied)"
        ),
    )
    p.add_argument("bundles", nargs="*", metavar="BUNDLE", help=bundle_help)
    p.add_argument(
        "dest_vault",
        metavar="VAULT",
        help="destination vault root; must already exist (an unmounted disk is not a vault)",
    )
    p.add_argument(
        "--all",
        action="store_true",
        help="copy every registered bundle in the source vault",
    )
    _add_dry_run(p, "print where each copy would land and what it costs; copy nothing")
    p.set_defaults(func=cmd_cp)

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
        "--handoff",
        action="store_true",
        help=(
            "hand verified bytes to the destination: after dest has a file as "
            "verified (ledger + size, or hashed under --rehash), delete the "
            "source copy and record it handed_off — the source becomes a skeleton"
        ),
    )
    p.add_argument(
        "--rehash",
        action="store_true",
        help=(
            "re-hash every dest payload file instead of trusting verified "
            "ledger entries (same as archive --rehash). On a network mount "
            "this reads the whole dest over the wire; run it on the dest host"
        ),
    )
    _add_dry_run(p, "print what would be copied, hashed, and released; change nothing")
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
    parse_argv = list(sys.argv[1:] if argv is None else argv)
    try:
        args = parser.parse_args(parse_argv)
    except SystemExit as exc:
        # `doctor` has a documented sysexits-style contract. Keep the legacy
        # argparse exit for every other command, but map doctor usage errors.
        if exc.code == 2 and "doctor" in parse_argv:
            return 64
        raise
    if not getattr(args, "command", None):
        parser.print_help()
        return 0
    args.argv = parse_argv  # a dry run ends by echoing this, minus the flag
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
