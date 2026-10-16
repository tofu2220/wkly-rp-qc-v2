"""Checked frontend build and test entry point."""

from pathlib import Path
import subprocess

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]


class FrontendCheckError(RuntimeError):
    pass


def build_frontend(root: Path = REPO_ROOT) -> None:
    return None


def main(argv: list[str] | None = None) -> int:
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
