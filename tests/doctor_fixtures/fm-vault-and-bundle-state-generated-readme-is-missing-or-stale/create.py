from __future__ import annotations

import sys
from pathlib import Path

from darsay import sources
from darsay.archiver import archive
from tests.fakes import TestProvider
from tests.payloads import model_files


def main() -> None:
    vault = Path(sys.argv[1])
    provider = TestProvider()
    provider.add_repo("acme/toy", model_files())
    sources.register_provider(provider)
    archive("test:acme/toy", vault=vault, progress=lambda *args: None, jobs=1)


if __name__ == "__main__":
    main()
