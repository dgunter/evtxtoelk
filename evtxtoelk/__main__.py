"""Allow ``python -m evtxtoelk``."""

import sys

from evtxtoelk.cli import main

if __name__ == "__main__":
    sys.exit(main())
