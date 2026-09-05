"""Live GitHub: one tiny public repository, pinned, fetched, verified.

Opt-in (``pytest --run-e2e`` or ``DARSAY_E2E=1``). ``octocat/Hello-World``
is GitHub's own one-file fixture repository and has not changed in years.
"""

from __future__ import annotations

from darsay.archiver import archive, load_manifest
from darsay.estimate import estimate
from darsay.verify import verify_bundle
from tests.conftest import silent

REPO = "github:octocat/Hello-World"


def test_estimate_and_archive_a_public_repository(vault):
    est = estimate(REPO, vault=vault, progress=silent)
    assert est["artifact_type"] == "code"
    assert est["payload"]["file_count"] >= 1
    assert est["source"]["revision_ref"] == "HEAD"
    assert len(est["source"]["revision"]) == 40

    bundle = archive(REPO, vault=vault, progress=silent, jobs=1)
    assert bundle is not None
    m = load_manifest(bundle)
    assert m["artifact_type"] == "code"
    assert m["source"]["provider"] == "github"
    assert m["source"]["revision"] == est["source"]["revision"]
    assert (bundle / "code" / "README").read_bytes().startswith(b"Hello World!")
    assert m["validation"]["checksum_verification"]["status"] == "pass"
    assert m["inventory"]["files"][0]["upstream_git_sha1"]
    assert verify_bundle(bundle, progress=silent)["result"] == "pass"
