# .mvb.tar — single-file bundle format (v1.0)

A modelvault export packs one bundle into one file for offsite storage and
transfer. The format is a plain tar, deliberately boring: any standard tar
tool can list and unpack it decades from now, with or without modelvault.

## Container

- **Filename:** `<bundle_id>.mvb.tar`, e.g. `qwen--qwen3-0.6b@c1899de289a0.mvb.tar`.
- **Format:** GNU tar, **uncompressed**. Model weights are essentially
  incompressible, so compression would cost inspectability for ~no size win.
  (A compressed variant, if ever added, will bump the format version.)
- **Entry order:** the marker `<bundle_id>/.mvb.json` first, then every bundle
  file as `<bundle_id>/<path>`, sorted by path. Only regular files — no
  directory entries, no symlinks (export refuses bundles containing them).
- **Excluded from the tar:** `exports.json` (see Determinism) and `.DS_Store`.

## Marker (`.mvb.json`, always the first entry)

```json
{
  "mvb_format_version": "1.0",
  "bundle_id": "qwen--qwen3-0.6b@c1899de289a0",
  "artifact_type": "model",
  "schema_version": "1.0.0",
  "bundle_hash": {"algorithm": "sha256-of-sorted-sha256-lines", "value": "…", "covers": "…"},
  "payload_file_count": 10,
  "payload_size_bytes": 1519114970,
  "written_by": {"tool": "modelvault"}
}
```

Being first, the marker can be read as a stream before committing to a
multi-gigabyte unpack: format compatibility, identity, and the expected
payload hash are known up front.

## Determinism

The same bundle state always produces a **byte-identical** tar, so an export
file has a single stable SHA-256 suitable for an offsite catalog or a museum
label. Guaranteed by:

- fixed entry order (marker, then sorted paths);
- normalized tar metadata on every entry: `mtime` = the bundle's
  `archive.date_archived`, `uid`/`gid` = 0, empty `uname`/`gname`, mode `0644`;
- **no volatile content inside the tar**: the export *event* (timestamp, tar
  sha256, destination) is appended to the bundle's `exports.json`, which is
  excluded from the tar precisely so that exporting doesn't change what the
  next export contains.

Scope of the guarantee: same bundle state and same format version. Bundle-root
metadata is mutable by design (a `verify` run updates the manifest), and any
such change legitimately changes the export bytes — the payload hash inside
is what stays constant.

## Versioning

Three independent layers:

| Layer | Field | Rule |
|---|---|---|
| Container | `mvb_format_version` in the marker | Import requires a matching **major** version. |
| Record | `schema_version` in the embedded manifest | Interpreted per [MANIFEST.md](MANIFEST.md). |
| Content | the pinned commit in `bundle_id` | Different upstream revisions are different bundles. |

## Import procedure (what `modelvault import` guarantees)

1. Stream the first entry; refuse anything whose leading entry is not a
   compatible `.mvb.json` marker.
2. Hash the tar file itself (recorded as import provenance).
3. Unpack into a staging directory using Python's safe `data` extraction
   filter (no path traversal, no specials).
4. Re-hash **every payload file** and compare against the embedded manifest's
   inventory; recompute the bundle hash and compare against the marker.
5. Only on a clean pass: rewrite `archive.location`/`host`, stamp
   `archive.imported = {at, from_file, file_sha256, mvb_format_version}`, move
   the bundle into the vault, and run a full `verify` to refresh
   `VERIFICATION.md`/`verification.json` at the new location.
6. On any failure: exit non-zero, remove staging, register nothing.

A corrupted archive — even a single flipped byte anywhere in the weights — is
therefore refused at import time, before the bundle can enter the vault.

## Manual recovery without modelvault

```bash
tar -tf qwen--qwen3-0.6b@c1899de289a0.mvb.tar     # list
tar -xf qwen--qwen3-0.6b@c1899de289a0.mvb.tar     # unpack
# integrity: hash model/* and compare with inventory.files[].sha256 in manifest.json
```
