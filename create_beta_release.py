"""Publish a beta: increment only the patch number; never trigger app updates."""
from scripts.release_common import main


if __name__ == "__main__":
    raise SystemExit(main("beta"))
