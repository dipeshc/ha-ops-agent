"""`python -m opsd daemon` (s6 service) or `checkpoint` (backup_pre hook)."""

import argparse
import asyncio
import sys

from opsd.config import Options, Paths, claude_token
from opsd.daemon import Daemon
from opsd.logs import setup_logging
from opsd.redact import build_redactor
from opsd.store import checkpoint_main
from opsd.supervisor import SupervisorClient, load_supervisor_token


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="opsd")
    parser.add_argument("command", choices=["daemon", "checkpoint"])
    args = parser.parse_args(argv)

    paths = Paths.from_env()
    if args.command == "checkpoint":
        # Runs from the Supervisor's backup_pre hook, which may lack the token.
        return checkpoint_main(paths.store_file)
    options = Options.load(paths)
    client = SupervisorClient(load_supervisor_token())
    secrets = [claude_token(options, paths), client.token]
    setup_logging(options.limits.log_level, build_redactor(paths.secrets_yaml, secrets))
    options.log_notes()

    asyncio.run(Daemon(paths, options, client).run_forever())
    return 0


if __name__ == "__main__":
    sys.exit(main())
