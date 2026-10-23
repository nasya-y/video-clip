"""Entry script for the frozen (PyInstaller) application."""

import sys

from vcm.cli import main

if __name__ == "__main__":
    sys.exit(main())
