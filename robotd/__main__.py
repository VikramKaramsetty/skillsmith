"""Entry point for `python -m robotd`. OWNER: WS1 (WS1.1 builds the server)."""

import sys


def main() -> int:
    print("robotd not built yet (WS1.1): see docs/workstreams/WS1_robotd_real.md", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
