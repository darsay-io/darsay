"""Artifact-type registry and completeness rules.

New artifact types (datasets, standalone GGUF packs, papers, ...) slot in by
adding a registry entry: what files a complete bundle must contain and which
are recommended. The manifest carries `artifact_type` so consumers can dispatch.
"""

from __future__ import annotations

from fnmatch import fnmatch

# artifact_type -> completeness rules. Each rule is (label, [glob patterns]);
# the rule passes when any pattern matches at least one inventory path.
ARTIFACT_TYPES = {
    "model": {
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
}

# Files the tool itself writes at the bundle root.
BUNDLE_METADATA_FILES = ["manifest.json", "README.md", "VERIFICATION.md", "verification.json", "curation.md"]


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
