"""python -m tradealerts: the same as the tradealerts command (used by the Windows background task)."""

import sys

from .cli import main

sys.exit(main())
