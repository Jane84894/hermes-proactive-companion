"""Consistent private backups of the companion SQLite database."""

from __future__ import annotations

import os
import sqlite3
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo


def backup_database(
    source_path: str | Path,
    backup_directory: str | Path,
    *,
    now: datetime | None = None,
    keep: int = 14,
) -> Path:
    source_path = Path(source_path).expanduser().resolve()
    backup_directory = Path(backup_directory).expanduser().resolve()
    if keep < 1:
        raise ValueError("keep must be positive")
    if not source_path.is_file():
        raise FileNotFoundError("companion database does not exist")
    backup_directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(backup_directory, 0o700)
    stamp = (now or datetime.now(ZoneInfo("UTC"))).strftime("%Y%m%d-%H%M%S")
    destination = backup_directory / f"companion-{stamp}.sqlite3"
    source = sqlite3.connect(f"{source_path.as_uri()}?mode=ro", uri=True, timeout=10)
    target = sqlite3.connect(destination, timeout=10)
    try:
        source.backup(target)
        target.commit()
    finally:
        target.close()
        source.close()
    os.chmod(destination, 0o600)
    backups = sorted(backup_directory.glob("companion-????????-??????.sqlite3"))
    for old_backup in backups[:-keep]:
        old_backup.unlink(missing_ok=True)
    return destination


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(prog="hermes-companion-backup")
    parser.add_argument("--source", default=str(Path.home() / ".hermes/companion/data/companion.sqlite3"))
    parser.add_argument("--directory", default=str(Path.home() / ".hermes/companion/backups"))
    args = parser.parse_args()
    path = backup_database(args.source, args.directory)
    print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
