"""Allow ``python -m mgit``."""

import sys

from mgit.cli import main

if __name__ == "__main__":
    sys.exit(main())
