"""Entry point for the bundled engine (the same `tradealerts` command, frozen with PyInstaller)."""
import multiprocessing
import sys

from tradealerts.cli import main

if __name__ == "__main__":
    multiprocessing.freeze_support()
    sys.exit(main())
