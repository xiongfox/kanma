"""允许 `python -m kanma <subcommand>` 直接调用 CLI。"""

from kanma.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
