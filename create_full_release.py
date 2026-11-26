"""Publish a signed full release: increment the minor number and reset patch to zero."""
from scripts.release_common import main


if __name__ == "__main__":
    raise SystemExit(main("full"))
