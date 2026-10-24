#!/usr/bin/env python3
# Copyright 2026 Canonical Ltd. This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Launch real omp ACP with disposable configuration, for isolated development only.

This is NOT a sandbox or a Workshop replacement. Never use it for production
reviews or untrusted checkouts on an ordinary host. ACP session cwd and native
file tools still have the current OS user's filesystem/network authority.
"""

import argparse
import json
import math
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from urllib.parse import urlsplit

OMP_VERSION = "18.12.2"


class Interrupted(RuntimeError):
    def __init__(self, signum: int):
        self.signum = signum
        super().__init__(f"Interrupted by signal {signum}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--omp", default="omp", help="Installed omp 18.12.2 executable")
    parser.add_argument("--model", required=True, help="Exact provider/model identity")
    parser.add_argument(
        "--gateway-url",
        required=True,
        help="Operator-approved inference gateway root URL",
    )
    parser.add_argument(
        "--gateway-token-env",
        default="PI_GATEWAY_TOKEN",
        help="Environment variable containing the gateway bearer "
        "(never a provider key)",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=120,
        help="Total local process deadline in seconds",
    )
    args = parser.parse_args()
    provider, separator, model = args.model.partition("/")
    if not separator or not provider or not model:
        parser.error("--model must be an exact provider/model identity")
    url = urlsplit(args.gateway_url)
    if (
        url.scheme not in ("http", "https")
        or not url.hostname
        or url.username
        or url.password
        or url.query
        or url.fragment
    ):
        parser.error(
            "--gateway-url must be an HTTP(S) URL without credentials, "
            "query, or fragment"
        )
    if not math.isfinite(args.timeout) or args.timeout <= 0:
        parser.error("--timeout must be positive")
    token = os.environ.get(args.gateway_token_env)
    if not token:
        parser.error(
            f"Gateway token environment variable {args.gateway_token_env!r} is empty"
        )
    executable = shutil.which(args.omp)
    if executable is None:
        parser.error(f"omp executable not found: {args.omp}")
    executable = str(Path(executable).resolve())

    def interrupt(signum, frame):
        raise Interrupted(signum)

    previous = {
        sig: signal.signal(sig, interrupt)
        for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)
    }
    deadline = time.monotonic() + args.timeout
    try:
        with tempfile.TemporaryDirectory(prefix="maas-acp-local-") as directory:
            root = Path(directory)
            home, agent, cwd = root / "home", root / "agent", root / "launch"
            for path in (home, agent, cwd, root / "tmp"):
                path.mkdir()
            env = {
                "PATH": "/usr/local/bin:/usr/bin:/bin",
                "HOME": str(home),
                "PI_CODING_AGENT_DIR": str(agent),
                "OMP_PROFILE": "",
                "XDG_CONFIG_HOME": str(root / "config"),
                "XDG_DATA_HOME": str(root / "data"),
                "XDG_STATE_HOME": str(root / "state"),
                "XDG_CACHE_HOME": str(root / "cache"),
                "XDG_RUNTIME_DIR": str(root / "run"),
                "TMPDIR": str(root / "tmp"),
                "SHELL": "/bin/sh",
                "TERM": "dumb",
                "ACP_PROBE_GATEWAY_TOKEN": token,
            }
            # JSON is valid YAML; no extra configuration-parser dependency.
            (agent / "models.yml").write_text(
                json.dumps(
                    {
                        "providers": {
                            provider: {
                                "baseUrl": args.gateway_url,
                                "transport": "pi-native",
                                "apiKey": "ACP_PROBE_GATEWAY_TOKEN",
                            }
                        }
                    }
                )
            )
            policy = root / "policy.yml"
            policy.write_text(
                json.dumps(
                    {
                        "tools": {"xdev": False},
                        "dev": {"autoqa": False},
                        "vault": {"enabled": False},
                        "security": {"enabled": False},
                    }
                )
            )
            version = subprocess.run(
                [executable, "--version"],
                env=env,
                cwd=cwd,
                capture_output=True,
                text=True,
                check=True,
                timeout=min(10, max(0, deadline - time.monotonic())),
            )
            if version.stdout.strip() != f"omp/{OMP_VERSION}":
                parser.error(
                    f"This probe requires omp {OMP_VERSION}; "
                    f"got {version.stdout.strip()!r}"
                )
            command = [
                executable,
                "acp",
                f"--model={args.model}",
                "--tools=read,grep,glob,web_search",
                "--no-extensions",
                "--no-skills",
                "--no-rules",
                "--no-lsp",
                "--no-pty",
                "--no-title",
                "--approval-mode=always-ask",
                f"--config={policy}",
                f"--max-time={max(0, deadline - time.monotonic())}s",
            ]
            print(
                "LOCAL ACP PROBE: not a sandbox; disposable state: " + str(root),
                file=sys.stderr,
                flush=True,
            )
            # Inherit protocol stdin/stdout unchanged; diagnostics stay on stderr.
            process = subprocess.Popen(
                command, env=env, cwd=cwd, start_new_session=True
            )
            try:
                returncode = process.wait(timeout=max(0, deadline - time.monotonic()))
            finally:
                # Own this process group only. Also reap children surviving omp's exit.
                try:
                    os.killpg(process.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    pass
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.wait()
            return returncode if returncode >= 0 else 128 - returncode
    except Interrupted as exc:
        return 128 + exc.signum
    except subprocess.TimeoutExpired:
        print("Local ACP probe timed out", file=sys.stderr)
        return 124
    except (OSError, subprocess.CalledProcessError) as exc:
        print(f"Local ACP probe failed: {exc}", file=sys.stderr)
        return 1
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)


if __name__ == "__main__":
    sys.exit(main())
