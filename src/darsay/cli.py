"""darsay — keep a model forever, run it tomorrow.

    pipx install darsay
    darsay archive Qwen/Qwen3-0.6B
    darsay run     qwen--qwen3-0.6b "Say hello"

A vault is a folder of bundles. A bundle is one pinned revision:
immutable payload, recorded facts, still loadable as-is.

    darsay estimate huggingface:Qwen/Qwen3-0.6B   preflight: size, params, disk — no download
    darsay estimate datasets/<owner>/<name>       Hugging Face shorthand; same for a dataset
    darsay archive huggingface:Qwen/Qwen3-0.6B    download + hash + manifest + reports
    darsay archive datasets/<owner>/<name>        archive a dataset (payload under data/)
    darsay verify  <bundle>           re-hash and compare against manifest
    darsay smoke   <bundle> [--inference]
    darsay list                       all bundles in the vault (id + path)
    darsay info    <bundle>           quick manifest summary
    darsay regen   <bundle>           rebuild README.md after editing curation.md
    darsay export  <bundle> [-o DIR]  pack into a single deterministic .mvb.tar
    darsay import  <file.mvb.tar>     unpack + verify into the vault
    darsay assemble <partial> […]     combine matching partials offline
    darsay hydrate <bundle>           build a runnable env for the bundle
    darsay run     <bundle> [PROMPT]  hydrate if needed, then generate (offline)
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


def _bundle_dir(args, spec: str | None = None, *, require_manifest: bool = True) -> Path:
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
        raise argparse.ArgumentTypeError(f"expected a positive number, got {value!r}") from exc
    if parsed <= 0:
        raise argparse.ArgumentTypeError(f"expected a positive number, got {value!r}")
    return parsed


def _byte_size(value: str) -> int:
    """Parse bytes with optional binary K/M/G/T suffixes (and optional B)."""
    match = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*([KMGT]?)\s*(?:I?B)?\s*", value.upper())
    if not match:
        raise argparse.ArgumentTypeError(
            f"invalid byte size {value!r}; use bytes or a suffix such as 500M or 20G"
        )
    number = float(match.group(1))
    multiplier = 1024 ** ("KMGT".index(match.group(2)) + 1) if match.group(2) else 1
    parsed = int(number * multiplier)
    if parsed <= 0:
        raise argparse.ArgumentTypeError(f"expected a positive byte size, got {value!r}")
    return parsed


def _positive_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"expected a positive integer, got {value!r}") from exc
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


def cmd_estimate(args) -> int:
    from .estimate import estimate, print_estimate

    est = estimate(
        args.source,
        revision=args.revision,
        vault=_vault_path(args, announce=True),
        include=args.include,
        variants=args.variants,
        progress=(lambda *a: None) if args.json else print,
    )
    if args.json:
        print(json.dumps(est, indent=2, ensure_ascii=False))
    else:
        print_estimate(est)
    return 1 if est["disk"]["verdict"] == "insufficient" else 0


def cmd_archive(args) -> int:
    from .archiver import archive
    from .transfer import PartialTransfer

    max_bytes = int(args.max_gb * 1024**3) if args.max_gb is not None else args.max_bytes
    try:
        bundle = archive(
            args.source,
            revision=args.revision,
            vault=_vault_path(args, announce=True),
            force=args.force,
            dry_run=args.dry_run,
            max_bytes=max_bytes,
            max_minutes=args.max_minutes,
            rehash=args.rehash,
            jobs=args.jobs,
            shard=args.shard,
            include=args.include,
        )
    except PartialTransfer as stop:
        print(f"\nArchive paused cleanly ({stop.reason}: {stop.detail}).")
        print(f"Partial bundle: {stop.bundle_dir}")
        print("Re-run the same archive command to continue from verified and partial bytes.")
        return 10
    if bundle is None:  # --dry-run printed the plan and intentionally did not register
        return 0
    print(f"\nBundle ready: {bundle}")
    print(f"  manifest:     {bundle / 'manifest.json'}")
    print(f"  readme:       {bundle / 'README.md'}")
    print(f"  verification: {bundle / 'VERIFICATION.md'}")
    print(f"  curation:     {bundle / 'curation.md'}  <- edit this, then `darsay regen`")
    return 0


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
    vault = _vault_path(args, announce=True)
    from .vault import bundle_id_for, iter_bundle_dirs

    bundle_dirs = iter_bundle_dirs(vault)
    if not bundle_dirs:
        print(f"No bundles in {vault}/")
        return 0
    from .readme_gen import human_size
    from .schema import payload_root_for
    from .transfer import LedgerError, load_ledger, transfer_plan

    rows = []
    for bundle_dir in bundle_dirs:
        manifest_path = bundle_dir / "manifest.json"
        if manifest_path.is_file():
            m = json.loads(manifest_path.read_text(encoding="utf-8"))
            rows.append((
                m["bundle_id"],
                str(bundle_dir),
                m["licensing"]["spdx_id"] or "?",
                human_size(m["inventory"]["total_size_bytes"]),
                m["security"]["integrity_status"],
                m["archive"]["date_archived"][:10],
            ))
            continue

        try:
            ledger = load_ledger(bundle_dir)
            root = payload_root_for(ledger["repo_type"])
            plan = transfer_plan(bundle_dir / root, ledger)
            sizes = plan["bytes"]
            files = plan["files"]
            banked = sizes["verified"] + sizes["partial"]
            percent = int(banked * 100 / sizes["total"]) if sizes["total"] else 0
            status = (
                f"archiving: {percent}% "
                f"({human_size(banked)}/{human_size(sizes['total'])}, "
                f"{files['verified']}/{files['total']} files verified)"
            )
            card = ledger.get("metadata", {}).get("card_data", {})
            license_id = card.get("license") if isinstance(card, dict) else None
            rows.append((
                bundle_id_for(bundle_dir),
                str(bundle_dir),
                license_id or "?",
                human_size(sizes["total"]),
                status,
                ledger["pinned_at"][:10],
            ))
        except (LedgerError, KeyError, OSError, TypeError, ValueError):
            rows.append((
                bundle_id_for(bundle_dir),
                str(bundle_dir),
                "?",
                "?",
                "archiving: unreadable transfer ledger",
                "?",
            ))
    header = ("BUNDLE", "PATH", "LICENSE", "SIZE", "INTEGRITY", "ARCHIVED")
    widths = [max(len(str(r[i])) for r in rows + [header]) for i in range(6)]
    for row in [header] + rows:
        print("  ".join(str(v).ljust(w) for v, w in zip(row, widths)))
    return 0


def cmd_info(args) -> int:
    from .archiver import load_manifest
    from .readme_gen import human_params, human_size

    bundle = _bundle_dir(args)
    m = load_manifest(bundle)
    print(f"{m['bundle_id']}  (schema v{m['schema_version']}, {m['artifact_type']})")
    src = m["source"]
    print(f"  source:     {src.get('address') or src['repo_id']} @ {src['revision'][:12]} ({src.get('provider') or src['origin']})")
    print(f"  license:    {m['licensing']['spdx_id']}  commercial={m['licensing']['commercial_use']}")
    if m["artifact_type"] == "dataset":
        dm = m["dataset_metadata"]
        fmts = ", ".join(f"{ext} x{d['file_count']}" for ext, d in (dm.get("formats") or {}).items())
        declared = (dm.get("declared") or {}).get("example_count_total")
        print(f"  formats:    {fmts or '?'}  declared examples={f'{declared:,}' if declared is not None else '?'}")
    else:
        meta = m["model_metadata"]
        print(f"  params:     {human_params(meta['parameter_count'])} {meta['precision'] or ''}  ctx={meta['context_length']}")
    print(f"  payload:    {m['inventory']['file_count']} files, {human_size(m['inventory']['total_size_bytes'])}")
    print(f"  integrity:  {m['security']['integrity_status']}  last check {m['archive']['last_integrity_check']}")
    smoke = m["validation"]["smoke_tests"]
    print("  smoke:      " + " ".join(f"{name}={r['status']}" for name, r in smoke.items()))
    if m["artifact_type"] != "dataset":
        from .hydrate import load_hydration

        hyd = load_hydration(bundle)
        if hyd:
            last = hyd["runs"][-1] if hyd.get("runs") else None
            run_note = (f"last run {last['status']} ({last['at'][:10]}, {last.get('tokens_per_second')} tok/s)"
                        if last else "no runs yet")
            print(f"  hydration:  {hyd['engine']} in env {hyd['env']['key']} — {run_note}")
        else:
            print(f"  hydration:  not hydrated (darsay hydrate {bundle})")
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
    )
    ledger = json.loads((bundle / "transfer.json").read_text(encoding="utf-8"))
    from .sources import source_from_ledger
    address = ledger.get("address") or source_from_ledger(ledger).canonical
    print(f"\nCombined partial bundle: {bundle}")
    if plan["complete"]:
        print("All payload files are present and verified; run archive once to register the bundle:")
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
        prompt=args.prompt,
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
        print(f"{env['key']}  python {env['python']}  {human_size(env['size_bytes'])}  created {env['created_at'][:10]}")
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


def main(argv=None) -> int:
    vault_help = "vault root (default: $DARSAY_HOME or ~/darsay)"
    # After the subcommand, SUPPRESS so a missing flag does not overwrite
    # `darsay --vault DIR list` with None.
    vault_after = argparse.ArgumentParser(add_help=False)
    vault_after.add_argument("--vault", default=argparse.SUPPRESS, help=vault_help)

    parser = argparse.ArgumentParser(prog="darsay", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--version", action="version", version=f"darsay {__version__}")
    parser.add_argument("--vault", default=None, help=vault_help)
    sub = parser.add_subparsers(dest="command", required=True)

    def add_cmd(name, **kwargs):
        kwargs.setdefault("parents", [vault_after])
        return sub.add_parser(name, **kwargs)

    p = add_cmd("estimate", help="preflight a source: size, params, disk headroom — no download")
    p.add_argument("source", help="e.g. huggingface:Qwen/Qwen3.8-27B, datasets/<owner>/<name>, or a huggingface.co URL")
    p.add_argument("--revision", help="branch, tag, or commit (default: main)")
    p.add_argument("--include", action="append", metavar="GLOB",
                   help="count only payload files matching GLOB (repeatable), "
                        "e.g. --include '*Q4_K_M*' to size one GGUF quant of a pack repo")
    p.add_argument("--variants", action="store_true",
                   help="also list upstream quantized variants of this model, with sizes")
    p.add_argument("--json", action="store_true", help="machine-readable output")
    p.set_defaults(func=cmd_estimate)

    p = add_cmd("archive", help="download and archive a model or dataset as a bundle")
    p.add_argument("source", help="e.g. huggingface:Qwen/Qwen3-0.6B, datasets/<owner>/<name>, or a huggingface.co URL")
    p.add_argument("--revision", help="branch, tag, or commit (default: main; always pinned to the resolved commit)")
    p.add_argument("--force", action="store_true", help="re-archive over an existing bundle")
    p.add_argument("--dry-run", action="store_true",
                   help="pin, reconcile, and print the transfer plan without moving payload bytes")
    budget = p.add_mutually_exclusive_group()
    budget.add_argument("--max-gb", type=_positive_float, metavar="N",
                        help="stop cleanly after approximately N GiB of network transfer")
    budget.add_argument("--max-bytes", type=_byte_size, metavar="SIZE",
                        help="stop cleanly after network SIZE (e.g. 500M, 20G)")
    p.add_argument("--max-minutes", type=_positive_float, metavar="N",
                   help="stop cleanly when the transfer session reaches N minutes")
    p.add_argument("--rehash", action="store_true",
                   help="re-hash every present payload file instead of trusting verified ledger entries")
    p.add_argument("--jobs", type=_positive_int, default=4, metavar="N",
                   help="parallel workers for files smaller than 8 MiB (default: 4)")
    p.add_argument("--shard", type=_shard_key, metavar="N/T",
                   help="advisory cooperative order: fetch byte-balanced lane N of T first")
    p.add_argument("--include", action="append", metavar="GLOB",
                   help="archive only payload files matching GLOB (repeatable), plus "
                        "sidecar files (config, tokenizer, license, card). "
                        "The manifest records the omitted upstream files")
    p.set_defaults(func=cmd_archive)

    bundle_help = "path, bundle id (name@revision12 from `list`), or a unique prefix"

    p = add_cmd("verify", help="re-hash a bundle and compare against its manifest")
    p.add_argument("bundle", help=bundle_help)
    p.set_defaults(func=cmd_verify)

    p = add_cmd("smoke", help="run smoke tests on a bundle")
    p.add_argument("bundle", help=bundle_help)
    p.add_argument("--inference", action="store_true", help="also load the model and generate (needs torch)")
    p.set_defaults(func=cmd_smoke)

    p = add_cmd("list", help="list bundles in the vault (id and copy-pasteable path)")
    p.set_defaults(func=cmd_list)

    p = add_cmd("info", help="summarize a bundle")
    p.add_argument("bundle", help=bundle_help)
    p.set_defaults(func=cmd_info)

    p = add_cmd("regen", help="rebuild a bundle's README.md from manifest + curation.md")
    p.add_argument("bundle", help=bundle_help)
    p.set_defaults(func=cmd_regen)

    p = add_cmd("hydrate", help="build (or reuse) a runnable local env for a bundle")
    p.add_argument("bundle", help=bundle_help)
    p.add_argument("--engine", help="runtime engine (default: auto-detect from the payload)")
    p.add_argument("--python", help="interpreter for the env (default: $DARSAY_PYTHON or this python)")
    p.add_argument("--weights", help="payload weights file for single-file engines, e.g. model/foo.gguf")
    p.add_argument("--force", action="store_true", help="rebuild the env even if it exists")
    p.add_argument("--dry-run", action="store_true", help="show the plan without touching anything")
    p.add_argument("--ignore-preflight", action="store_true",
                   help="try anyway if the architecture or RAM check fails")
    p.set_defaults(func=cmd_hydrate)

    p = add_cmd("run", help="run a prompt against a bundle (hydrates first if needed; fully offline)")
    p.add_argument("bundle", help=bundle_help)
    p.add_argument("prompt", nargs="?", help='prompt text (default: "Say hello in one short sentence.")')
    p.add_argument("--engine", help="runtime engine (default: the hydrated one, else auto-detect)")
    p.add_argument("--max-new-tokens", type=int, default=256)
    p.add_argument("--device", default="auto", help="auto | cpu | cuda | mps")
    p.add_argument("--dtype", default="auto", help="transformers only: auto | float32 | bfloat16 | float16")
    p.add_argument("--raw", action="store_true", help="plain completion — skip the chat template")
    p.add_argument("--sample", action="store_true", help="sample with the model's generation defaults (default: greedy)")
    p.add_argument("--seed", type=int, help="seed for --sample")
    p.add_argument("--trust-remote-code", action="store_true", help="allow custom modeling code from the payload")
    p.add_argument("--timeout", type=float, help="kill the run after N seconds")
    p.add_argument("--python", help="interpreter if hydration is needed")
    p.add_argument("--weights", help="weights file if hydration is needed (single-file engines)")
    p.add_argument("--ignore-preflight", action="store_true",
                   help="try anyway if the architecture or RAM check fails")
    p.set_defaults(func=cmd_run)

    p = add_cmd("dehydrate", help="remove a bundle's hydration record (envs are shared; prune via `envs --prune`)")
    p.add_argument("bundle", help=bundle_help)
    p.set_defaults(func=cmd_dehydrate)

    p = add_cmd("envs", help="list shared runtime envs and which bundles use them")
    p.add_argument("--prune", action="store_true", help="delete envs no hydrated bundle references")
    p.set_defaults(func=cmd_envs)

    p = add_cmd("export", help="pack a bundle into a single deterministic .mvb.tar file")
    p.add_argument("bundle", help=bundle_help)
    p.add_argument("-o", "--output-dir", default=".", help="directory for the .mvb.tar (default: cwd)")
    p.set_defaults(func=cmd_export)

    p = add_cmd("import", help="unpack a .mvb.tar into the vault, verifying before registering")
    p.add_argument("file")
    p.add_argument("--force", action="store_true", help="replace an existing bundle at the destination")
    p.set_defaults(func=cmd_import)

    p = add_cmd("assemble", help="combine matching partial bundles offline into this vault")
    p.add_argument("partials", nargs="+", metavar="BUNDLE",
                   help="partial bundle directories with the same pinned revision")
    p.set_defaults(func=cmd_assemble)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
