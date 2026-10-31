"""opsctl: the AI's only window onto the Supervisor, Core, git history and its effects.

This is a thin, unprivileged client. It holds no token and reads nothing
itself; its command line is generated from the capability registry, and it
sends one request to the daemon over a unix socket and prints the (already
redacted) answer. The daemon decides whether the request is allowed.
"""

import os
import sys
from pathlib import Path

from opsd import rpc
from opsd.capabilities import BY_VERB, build_parser

DEFAULT_SOCKET = "/run/ops-sock/opsd.sock"
MAX_OUTPUT_CHARS = rpc.MAX_OUTPUT_CHARS


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.verb == "help":
        if args.topic:
            sub = next(a for a in parser._actions if a.dest == "verb")
            sub.choices[args.topic].print_help()  # type: ignore[index]
        else:
            parser.print_help()
        return 0
    capability = BY_VERB[args.verb]
    request = {k: v for k, v in vars(args).items() if k in capability.args and v is not None}
    socket_path = Path(os.environ.get("OPS_SOCKET_DIR", os.path.dirname(DEFAULT_SOCKET))) / "opsd.sock"
    try:
        response = rpc.call(socket_path, args.verb, request)
    except (OSError, ValueError) as err:
        print(f"opsctl: cannot reach the ops daemon: {type(err).__name__}", file=sys.stderr)
        return 1
    if response.get("ok"):
        print(response.get("output", ""))
        return 0
    print(f"error: {response.get('error', 'unknown error')}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
