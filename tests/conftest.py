"""Shared test paths without adding a runtime dependency on pytest."""

from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"
