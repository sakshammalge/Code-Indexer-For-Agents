"""CLI entry point for the inventory service."""
import sys
from .models import Warehouse


def main(argv=None) -> int:
    wh = Warehouse("central")
    print(wh.name)
    return 0


if __name__ == "__main__":
    sys.exit(main())
