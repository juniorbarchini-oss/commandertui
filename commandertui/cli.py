from __future__ import annotations

import argparse
import os

from . import __version__
from .app import CommanderApp


def main() -> None:
    parser = argparse.ArgumentParser(prog="commandertui", description="Dual-pane TUI file manager")
    parser.add_argument("left", nargs="?", default=os.path.expanduser("~"))
    parser.add_argument("right", nargs="?", default=os.path.expanduser("~"))
    parser.add_argument("--version", action="version", version=f"commandertui {__version__}")
    args = parser.parse_args()

    app = CommanderApp(left_path=args.left, right_path=args.right)
    app.run()


if __name__ == "__main__":
    main()
