"""Allow `python -m clade serve ...` invocation. Console script entry
(`clade` binary) ce biti dodat u pyproject u PR#4."""

from clade.server import main

if __name__ == "__main__":
    raise SystemExit(main())
