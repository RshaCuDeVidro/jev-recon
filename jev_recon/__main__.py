"""``python -m jev_recon`` -> the same entry point as the ``jev-recon`` script."""

from .cli import main

if __name__ == "__main__":
    raise SystemExit(main())
