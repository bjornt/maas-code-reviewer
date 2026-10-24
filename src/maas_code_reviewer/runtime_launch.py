# Copyright 2026 Canonical Ltd. This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Trusted container-local exec helper; values of secrets never enter argv/files."""

import json
import os
import sys
from pathlib import Path


def launch(specification: Path) -> None:
    with specification.open(encoding="utf-8") as stream:
        spec = json.load(stream)
    environment = spec["environment"]
    for name in spec["inherit"]:
        value = os.environ.get(name)
        if not value:
            raise RuntimeError(f"Required runtime environment variable {name} is empty")
        environment[name] = value
    os.chdir(spec["cwd"])
    os.execve(spec["argv"][0], spec["argv"], environment)


if __name__ == "__main__":
    launch(Path(sys.argv[1]))
