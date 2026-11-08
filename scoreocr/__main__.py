"""Entry point for `python -m scoreocr`.

The code lives in cli.py: on Windows, worker processes cannot import functions defined in a
package's __main__ module.
"""

import sys

from .cli import main

if __name__ == "__main__":
    sys.exit(main())
