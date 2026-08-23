"""modelvault CLI.

    modelvault archive Qwen/Qwen3-0.6B          download + hash + manifest + reports
    modelvault verify  vault/<bundle>           re-hash and compare against manifest
    modelvault smoke   vault/<bundle> [--inference]
    modelvault list                             all bundles in the vault
    modelvault info    vault/<bundle>           quick manifest summary
    modelvault regen   vault/<bundle>           rebuild README.md after editing curation.md
    modelvault export  vault/<bundle> [-o DIR]  pack into a single deterministic .mvb.tar
    modelvault import  <file.mvb.tar>           unpack + verify into the vault
"""

from __future__ import annotations

import argparse
import json
import os
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


def cmd_archive(args) -> int:
    from .archiver import archive_model

    bundle = archive_model(
        repo_id=args.repo_id,
        revision=args.revision,
        vault=_vault_path(args),
        force=args.force,
    )
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
    meta = m["model_metadata"]
    print(f"{m['bundle_id']}  (schema v{m['schema_version']}, {m['artifact_type']})")
    print(f"  source:     {m['source']['repo_id']} @ {m['source']['revision'][:12]} ({m['source']['origin']})")
    print(f"  license:    {m['licensing']['spdx_id']}  commercial={m['licensing']['commercial_use']}")
    print(f"  params:     {human_params(meta['parameter_count'])} {meta['precision'] or ''}  ctx={meta['context_length']}")
    print(f"  payload:    {m['inventory']['file_count']} files, {human_size(m['inventory']['total_size_bytes'])}")
    print(f"  integrity:  {m['security']['integrity_status']}  last check {m['archive']['last_integrity_check']}")
    smoke = m["validation"]["smoke_tests"]
    print(f"  smoke:      tokenizer={smoke['tokenizer']['status']} inference={smoke['inference']['status']}")
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

    p = sub.add_parser("archive", help="download and archive a model repo as a bundle")
    p.add_argument("repo_id", help="e.g. Qwen/Qwen3-0.6B")
    p.add_argument("--revision", help="branch, tag, or commit (default: main; always pinned to the resolved commit)")
    p.add_argument("--force", action="store_true", help="re-archive over an existing bundle")
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
