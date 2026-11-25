"""``python -m pmr.audio ...`` is an alias of ``python -m pmr.audio.dataset ...``."""
import sys

from .dataset import main

if __name__ == "__main__":
    sys.exit(main())
