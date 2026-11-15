import sqlite3
from pathlib import Path

import pytest

from steam_scraper.database import DatabaseManager
from steam_scraper.models import GameInfo


def _create_legacy_database(path: Path) -> None:
    with sqlite3.connect(path) as connection:
        connection.execute(
            """
            CREATE TABLE games (
                app_id INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                release_date TEXT,
                price TEXT,
                developers TEXT,
                publishers TEXT,
                genres TEXT,
                description TEXT,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        connection.executemany(
            "INSERT INTO games (app_id, name, price, updated_at) VALUES (?, ?, ?, ?)",
            [
                (10, "Priced", "$9.99", "2025-01-02 03:04:05"),
                (20, "Free game", "Free", "2025-02-03 04:05:06"),
                (30, "No price", "", "2025-03-04 05:06:07"),
                (40, "Null price", None, "2025-04-05 06:07:08"),
                (50, "No time", "$4.99", None),
            ],
        )


def test_first_open_backfills_history_from_updated_at(tmp_path: Path) -> None:
    db_path = tmp_path / "legacy.db"
    _create_legacy_database(db_path)

    manager = DatabaseManager(db_path)
    try:
        rows = manager.conn.execute(
            "SELECT app_id, price, recorded_at FROM price_history ORDER BY app_id"
        ).fetchall()
    finally:
        manager.close()

    assert rows == [
        (10, "$9.99", "2025-01-02 03:04:05"),
        (20, "Free", "2025-02-03 04:05:06"),
    ]


def test_reopening_database_does_not_backfill_again(tmp_path: Path) -> None:
    db_path = tmp_path / "legacy.db"
    _create_legacy_database(db_path)

    DatabaseManager(db_path).close()
    manager = DatabaseManager(db_path)
    try:
        count = manager.conn.execute("SELECT COUNT(*) FROM price_history").fetchone()[0]
    finally:
        manager.close()

    assert count == 2


def test_legacy_backfill_never_runs_after_table_exists(tmp_path: Path) -> None:
    db_path = tmp_path / "legacy.db"
    _create_legacy_database(db_path)

    manager = DatabaseManager(db_path)
    try:
        manager.conn.execute("DELETE FROM price_history WHERE app_id = 10")
        manager.conn.commit()
    finally:
        manager.close()

    manager = DatabaseManager(db_path)
    try:
        assert manager.conn.execute(
            "SELECT app_id FROM price_history WHERE app_id = 10"
        ).fetchone() is None
    finally:
        manager.close()
