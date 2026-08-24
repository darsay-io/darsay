"""Artifact-type registry, payload roots, and completeness rules.

New artifact types (standalone GGUF packs, papers, ...) slot in by adding a
registry entry: where the payload lives, what files a complete bundle must
contain and which are recommended. The manifest carries `artifact_type` so
consumers can dispatch; `model` and `dataset` are the two current types.
"""

from __future__ import annotations

from fnmatch import fnmatch

# artifact_type -> payload root + completeness rules. Each rule is
# (label, [glob patterns]); the rule passes when any pattern matches at least
# one inventory path.
ARTIFACT_TYPES = {
    "model": {
        "payload_root": "model/",
        "required": [
            ("config", ["model/config.json", "model/*.gguf"]),
            ("weights", ["model/*.safetensors", "model/*.bin", "model/*.gguf", "model/*.pt", "model/*.pth"]),
            ("tokenizer", ["model/tokenizer.json", "model/tokenizer.model", "model/vocab.json", "model/spiece.model", "model/*.gguf"]),
        ],
        "recommended": [
            ("model_card", ["model/README.md", "model/README*.md"]),
            ("license", ["model/LICENSE*", "model/LICENCE*", "model/COPYING*", "model/license*"]),
            ("generation_config", ["model/generation_config.json"]),
            ("tokenizer_config", ["model/tokenizer_config.json"]),
        ],
    },
    "dataset": {
        "payload_root": "data/",
        "required": [
            # fnmatch '*' crosses '/', so these match nested files too.
            ("data", ["data/*.parquet", "data/*.jsonl", "data/*.json", "data/*.csv",
                      "data/*.arrow", "data/*.txt", "data/*.tsv"]),
        ],
        "recommended": [
            ("dataset_card", ["data/README.md", "data/README*.md"]),
            ("license", ["data/LICENSE*", "data/LICENCE*", "data/COPYING*", "data/license*"]),
            ("dataset_infos", ["data/dataset_infos.json"]),
        ],
    },
}

# Files the tool itself writes at the bundle root.
BUNDLE_METADATA_FILES = ["manifest.json", "README.md", "VERIFICATION.md", "verification.json",
                         "curation.md", "exports.json", "hydration.json", "transfer.json",
                         "transfer.lock"]


def payload_root_for(artifact_type: str) -> str:
    """Payload directory name (no trailing slash) for an artifact type — for
    writers creating a bundle. Readers of existing bundles use payload_root()."""
    return ARTIFACT_TYPES[artifact_type]["payload_root"].rstrip("/")


def payload_root(manifest: dict) -> str:
    """Payload directory name (no trailing slash) recorded in a manifest.
    Falls back to "model/" for pre-1.2 manifests that predate the field."""
    layout = manifest.get("inventory", {}).get("layout") or {}
    return (layout.get("payload_root") or "model/").rstrip("/")


def check_completeness(artifact_type: str, inventory_paths: list[str]) -> dict:
    rules = ARTIFACT_TYPES.get(artifact_type)
    if rules is None:
        return {"status": "unknown-artifact-type", "artifact_type": artifact_type}

    def evaluate(rule_set):
        results = {}
        for label, patterns in rule_set:
            matched = sorted(
                {path for path in inventory_paths for pat in patterns if fnmatch(path, pat)}
            )
            results[label] = {"present": bool(matched), "matched": matched[:10]}
        return results

    required = evaluate(rules["required"])
    recommended = evaluate(rules["recommended"])
    missing_required = [label for label, r in required.items() if not r["present"]]
    missing_recommended = [label for label, r in recommended.items() if not r["present"]]
    return {
        "status": "complete" if not missing_required else "incomplete",
        "required": required,
        "recommended": recommended,
        "missing_required": missing_required,
        "missing_recommended": missing_recommended,
    }
