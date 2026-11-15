import sqlite3
import threading
from pathlib import Path

import pytest

from steam_scraper.database import DatabaseManager
from steam_scraper.models import GameInfo


def _history(manager: DatabaseManager, app_id: int | None = None) -> list[tuple]:
    query = "SELECT app_id, price FROM price_history"
    parameters: tuple = ()
    if app_id is not None:
        query += " WHERE app_id = ?"
        parameters = (app_id,)
    query += " ORDER BY rowid"
    return manager.conn.execute(query, parameters).fetchall()


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


def test_price_change_appends_and_same_price_is_skipped(database: DatabaseManager) -> None:
    database.save_game(GameInfo(10, "Game", price="$9.99"))
    database.save_game(GameInfo(10, "Game", price="$9.99"))
    database.save_game(GameInfo(10, "Game", price="Free"))
    database.save_game(GameInfo(10, "Game", price="Free"))
    database.save_game(GameInfo(10, "Game", price="$4.99"))

    assert _history(database, 10) == [
        (10, "$9.99"),
        (10, "Free"),
        (10, "$4.99"),
    ]


def test_empty_price_is_not_recorded_and_keeps_last_known(
    database: DatabaseManager,
) -> None:
    database.save_game(GameInfo(10, "Game", price="$9.99"))
    database.save_game(GameInfo(10, "Game", price=""))
    database.save_game(GameInfo(10, "Game", price="   "))
    database.save_game(GameInfo(10, "Game", price="$9.99"))

    assert _history(database, 10) == [(10, "$9.99")]


def test_first_non_empty_price_starts_history(database: DatabaseManager) -> None:
    database.save_game(GameInfo(10, "Game", price=""))
    database.save_game(GameInfo(10, "Game", price="$9.99"))

    assert _history(database, 10) == [(10, "$9.99")]


def test_price_is_stored_verbatim(database: DatabaseManager) -> None:
    database.save_game(GameInfo(10, "Game", price="¥ 9,99 / 1,00 €"))

    assert _history(database, 10) == [(10, "¥ 9,99 / 1,00 €")]


def test_recorded_at_uses_utc_like_games_updated_at(database: DatabaseManager) -> None:
    database.save_game(GameInfo(10, "Game", price="$9.99"))

    history_time, game_time = database.conn.execute(
        """
        SELECT p.recorded_at, g.updated_at
        FROM price_history p JOIN games g USING (app_id)
        """
    ).fetchone()
    assert history_time == game_time
    assert history_time.endswith("Z") is False
    assert len(history_time) == 19


def test_price_history_failure_rolls_back_game(database: DatabaseManager) -> None:
    database.save_game(GameInfo(10, "Original", price="$9.99"))

    # 让历史写入失败：删除表，保存事务内 INSERT 将报错。
    database.conn.execute("ALTER TABLE price_history RENAME TO price_history_gone")

    with pytest.raises(sqlite3.Error):
        database.save_game(GameInfo(10, "Changed", price="$4.99"))

    assert database.conn.execute(
        "SELECT name, price FROM games WHERE app_id = 10"
    ).fetchone() == ("Original", "$9.99")


def test_batch_failure_rolls_back_whole_batch(database: DatabaseManager) -> None:
    database.save_game(GameInfo(10, "Original", price="$9.99"))

    original_append = database._append_price_history
    calls = {"count": 0}

    def fail_second_append(game: GameInfo) -> None:
        calls["count"] += 1
        if calls["count"] == 2:
            raise sqlite3.OperationalError("simulated history failure")
        original_append(game)

    database._append_price_history = fail_second_append  # type: ignore[method-assign]
    try:
        with pytest.raises(sqlite3.OperationalError, match="simulated"):
            database.save_games_batch(
                [
                    GameInfo(10, "Changed", price="$4.99"),
                    GameInfo(20, "New", price="$1.99"),
                    GameInfo(30, "Never", price="$2.99"),
                ]
            )
    finally:
        del database._append_price_history

    assert database.conn.execute("SELECT app_id FROM games ORDER BY app_id").fetchall() == [
        (10,)
    ]
    assert _history(database) == [(10, "$9.99")]


def test_concurrent_saves_of_same_price_keep_one_row(tmp_path: Path) -> None:
    database = DatabaseManager(tmp_path / "concurrent.db")
    barrier = threading.Barrier(8)

    def save() -> None:
        barrier.wait()
        for _ in range(20):
            database.save_game(GameInfo(10, "Game", price="$9.99"))

    threads = [threading.Thread(target=save) for _ in range(8)]
    try:
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert _history(database, 10) == [(10, "$9.99")]
    finally:
        database.close()


def test_concurrent_saves_of_different_games_do_not_interfere(tmp_path: Path) -> None:
    database = DatabaseManager(tmp_path / "concurrent.db")
    barrier = threading.Barrier(3)

    def save(app_id: int, start: int) -> None:
        barrier.wait()
        for index in range(start, start + 10):
            database.save_game(GameInfo(app_id, f"Game {app_id}", price=f"${index}"))

    threads = [
        threading.Thread(target=save, args=(10, 0)),
        threading.Thread(target=save, args=(20, 100)),
        threading.Thread(target=save, args=(30, 200)),
    ]
    try:
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        for app_id, start in ((10, 0), (20, 100), (30, 200)):
            assert _history(database, app_id) == [
                (app_id, f"${index}") for index in range(start, start + 10)
            ]
    finally:
        database.close()
