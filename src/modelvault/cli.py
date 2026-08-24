"""modelvault CLI.

    modelvault estimate Qwen/Qwen3-0.6B         preflight: size, params, disk — no download
    modelvault estimate datasets/<owner>/<name>  same, for a dataset repo
    modelvault archive Qwen/Qwen3-0.6B          download + hash + manifest + reports
    modelvault archive datasets/<owner>/<name>  archive a dataset (payload under data/)
    modelvault verify  vault/<bundle>           re-hash and compare against manifest
    modelvault smoke   vault/<bundle> [--inference]
    modelvault list                             all bundles in the vault
    modelvault info    vault/<bundle>           quick manifest summary
    modelvault regen   vault/<bundle>           rebuild README.md after editing curation.md
    modelvault export  vault/<bundle> [-o DIR]  pack into a single deterministic .mvb.tar
    modelvault import  <file.mvb.tar>           unpack + verify into the vault
    modelvault hydrate vault/<bundle>           build a runnable env for the bundle
    modelvault run     vault/<bundle> [PROMPT]  hydrate if needed, then generate (offline)
    modelvault dehydrate vault/<bundle>         drop the bundle's hydration record
    modelvault envs [--prune]                   list / clean up shared runtime envs

Repo refs use the Hub's own grammar: `owner/name` is a model,
`datasets/owner/name` a dataset, and either huggingface.co URL form works.
Bundle-path commands dispatch on the manifest's artifact_type.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

from . import __version__


def _vault_path(args) -> Path:
    return Path(args.vault or os.environ.get("MODELVAULT_HOME") or "vault")


def _bundle_dir(path_str: str) -> Path:
    bundle = Path(path_str)
    if not (bundle / "manifest.json").is_file():
        sys.exit(f"error: no manifest.json in {bundle} — not a modelvault bundle")
    return bundle


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


def cmd_estimate(args) -> int:
    from .archiver import parse_repo_ref
    from .estimate import estimate_repo, print_estimate

    repo_type, repo_id = parse_repo_ref(args.repo_id)
    est = estimate_repo(
        repo_id,
        revision=args.revision,
        vault=_vault_path(args),
        include=args.include,
        variants=args.variants,
        repo_type=repo_type,
        progress=(lambda *a: None) if args.json else print,
    )
    if args.json:
        print(json.dumps(est, indent=2, ensure_ascii=False))
    else:
        print_estimate(est)
    return 1 if est["disk"]["verdict"] == "insufficient" else 0


def cmd_archive(args) -> int:
    from .archiver import archive_model, parse_repo_ref
    from .transfer import PartialTransfer

    repo_type, repo_id = parse_repo_ref(args.repo_id)
    max_bytes = int(args.max_gb * 1024**3) if args.max_gb is not None else args.max_bytes
    try:
        bundle = archive_model(
            repo_id=repo_id,
            revision=args.revision,
            vault=_vault_path(args),
            force=args.force,
            repo_type=repo_type,
            dry_run=args.dry_run,
            max_bytes=max_bytes,
            max_minutes=args.max_minutes,
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
    print(f"  curation:     {bundle / 'curation.md'}  <- edit this, then `modelvault regen`")
    return 0


def cmd_verify(args) -> int:
    from .verify import verify_bundle

    report = verify_bundle(_bundle_dir(args.bundle))
    return 0 if report["result"] == "pass" else 1


def cmd_smoke(args) -> int:
    from .smoke import run_smoke

    results = run_smoke(_bundle_dir(args.bundle), inference=args.inference)
    failed = any(r.get("status") == "fail" for r in results.values())
    print(json.dumps(results, indent=2))
    return 1 if failed else 0


def cmd_list(args) -> int:
    vault = _vault_path(args)
    manifests = sorted(vault.glob("*/*/manifest.json"))
    if not manifests:
        print(f"No bundles in {vault}/")
        return 0
    from .readme_gen import human_size

    rows = []
    for mf in manifests:
        m = json.loads(mf.read_text(encoding="utf-8"))
        rows.append((
            m["bundle_id"],
            m["licensing"]["spdx_id"] or "?",
            human_size(m["inventory"]["total_size_bytes"]),
            m["security"]["integrity_status"],
            m["archive"]["date_archived"][:10],
        ))
    widths = [max(len(str(r[i])) for r in rows + [("BUNDLE", "LICENSE", "SIZE", "INTEGRITY", "ARCHIVED")])
              for i in range(5)]
    header = ("BUNDLE", "LICENSE", "SIZE", "INTEGRITY", "ARCHIVED")
    for row in [header] + rows:
        print("  ".join(str(v).ljust(w) for v, w in zip(row, widths)))
    return 0


def cmd_info(args) -> int:
    from .archiver import load_manifest, utc_now, write_manifest
    from .readme_gen import human_params, human_size

    bundle = _bundle_dir(args.bundle)
    m = load_manifest(bundle)
    print(f"{m['bundle_id']}  (schema v{m['schema_version']}, {m['artifact_type']})")
    print(f"  source:     {m['source']['repo_id']} @ {m['source']['revision'][:12]} ({m['source']['origin']})")
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
            print(f"  hydration:  not hydrated (modelvault hydrate {bundle})")
    m["archive"]["last_accessed"] = utc_now()
    write_manifest(bundle, m)
    return 0


def cmd_export(args) -> int:
    from .export import export_bundle

    out = export_bundle(_bundle_dir(args.bundle), Path(args.output_dir))
    print(f"Export ready: {out}")
    return 0


def cmd_import(args) -> int:
    from .export import import_bundle

    import_bundle(Path(args.file), _vault_path(args), force=args.force)
    return 0


def cmd_hydrate(args) -> int:
    from .hydrate import hydrate_bundle

    record = hydrate_bundle(
        _bundle_dir(args.bundle),
        engine=args.engine,
        python=args.python,
        weights=args.weights,
        force=args.force,
        dry_run=args.dry_run,
    )
    if record.get("dry_run"):
        return 0
    return 0 if record["probe"].get("status") == "pass" else 1


def cmd_run(args) -> int:
    from .hydrate import run_bundle

    record = run_bundle(
        _bundle_dir(args.bundle),
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
    )
    return 0 if record["status"] == "pass" else 1


def cmd_dehydrate(args) -> int:
    from .hydrate import dehydrate_bundle

    dehydrate_bundle(_bundle_dir(args.bundle))
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
        print(f"No runtime envs under {vault}/.runtime/envs/ (or $MODELVAULT_RUNTIME)")
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

    bundle = _bundle_dir(args.bundle)
    write_bundle_readme(bundle, load_manifest(bundle))
    print(f"Regenerated {bundle / 'README.md'}")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="modelvault", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--version", action="version", version=f"modelvault {__version__}")
    parser.add_argument("--vault", help="vault root (default: $MODELVAULT_HOME or ./vault)")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("estimate", help="preflight a repo: size, params, disk headroom — no download")
    p.add_argument("repo_id", help="e.g. Qwen/Qwen3.8-27B, datasets/<owner>/<name>, or a huggingface.co URL")
    p.add_argument("--revision", help="branch, tag, or commit (default: main)")
    p.add_argument("--include", action="append", metavar="GLOB",
                   help="count only payload files matching GLOB (repeatable), "
                        "e.g. --include '*Q4_K_M*' to size one GGUF quant of a pack repo")
    p.add_argument("--variants", action="store_true",
                   help="also list upstream quantized variants of this model, with sizes")
    p.add_argument("--json", action="store_true", help="machine-readable output")
    p.set_defaults(func=cmd_estimate)

    p = sub.add_parser("archive", help="download and archive a model or dataset repo as a bundle")
    p.add_argument("repo_id", help="e.g. Qwen/Qwen3-0.6B, datasets/<owner>/<name>, or a huggingface.co URL")
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
    p.set_defaults(func=cmd_archive)

    p = sub.add_parser("verify", help="re-hash a bundle and compare against its manifest")
    p.add_argument("bundle")
    p.set_defaults(func=cmd_verify)

    p = sub.add_parser("smoke", help="run smoke tests on a bundle")
    p.add_argument("bundle")
    p.add_argument("--inference", action="store_true", help="also load the model and generate (needs torch)")
    p.set_defaults(func=cmd_smoke)

    p = sub.add_parser("list", help="list bundles in the vault")
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("info", help="summarize a bundle")
    p.add_argument("bundle")
    p.set_defaults(func=cmd_info)

    p = sub.add_parser("regen", help="rebuild a bundle's README.md from manifest + curation.md")
    p.add_argument("bundle")
    p.set_defaults(func=cmd_regen)

    p = sub.add_parser("hydrate", help="build (or reuse) a runnable local env for a bundle")
    p.add_argument("bundle")
    p.add_argument("--engine", help="runtime engine (default: auto-detect from the payload)")
    p.add_argument("--python", help="interpreter for the env (default: $MODELVAULT_PYTHON or this python)")
    p.add_argument("--weights", help="payload weights file for single-file engines, e.g. model/foo.gguf")
    p.add_argument("--force", action="store_true", help="rebuild the env even if it exists")
    p.add_argument("--dry-run", action="store_true", help="show the plan without touching anything")
    p.set_defaults(func=cmd_hydrate)

    p = sub.add_parser("run", help="run a prompt against a bundle (hydrates first if needed; fully offline)")
    p.add_argument("bundle")
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
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("dehydrate", help="remove a bundle's hydration record (envs are shared; prune via `envs --prune`)")
    p.add_argument("bundle")
    p.set_defaults(func=cmd_dehydrate)

    p = sub.add_parser("envs", help="list shared runtime envs and which bundles use them")
    p.add_argument("--prune", action="store_true", help="delete envs no hydrated bundle references")
    p.set_defaults(func=cmd_envs)

    p = sub.add_parser("export", help="pack a bundle into a single deterministic .mvb.tar file")
    p.add_argument("bundle")
    p.add_argument("-o", "--output-dir", default=".", help="directory for the .mvb.tar (default: cwd)")
    p.set_defaults(func=cmd_export)

    p = sub.add_parser("import", help="unpack a .mvb.tar into the vault, verifying before registering")
    p.add_argument("file")
    p.add_argument("--force", action="store_true", help="replace an existing bundle at the destination")
    p.set_defaults(func=cmd_import)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
