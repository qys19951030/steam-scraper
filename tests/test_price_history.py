import csv
import os
import sqlite3
import threading
from pathlib import Path

from datetime import date

import pytest
from openpyxl import load_workbook

from steam_scraper.database import DatabaseManager
from steam_scraper.models import GameInfo, ReviewSnapshot


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


def test_price_change_appends_and_same_price_is_skipped(
    database: DatabaseManager,
) -> None:
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

    assert database.conn.execute(
        "SELECT app_id FROM games ORDER BY app_id"
    ).fetchall() == [(10,)]
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


def test_get_price_history_orders_by_app_id_and_time(database: DatabaseManager) -> None:
    database.save_game(GameInfo(20, "Second", price="$1.99"))
    database.save_game(GameInfo(10, "First", price="$9.99"))
    database.save_game(GameInfo(10, "First", price="Free"))

    rows = database.get_price_history()

    assert [row[:3] for row in rows] == [
        (10, "First", "$9.99"),
        (10, "First", "Free"),
        (20, "Second", "$1.99"),
    ]
    assert all(len(row[3]) == 19 for row in rows)


def test_get_price_history_without_rows_returns_empty(
    database: DatabaseManager,
) -> None:
    assert database.get_price_history() == []


def _read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as stream:
        return list(csv.DictReader(stream))


def test_excel_adds_price_history_sheet_only_when_present(
    database: DatabaseManager, tmp_path: Path
) -> None:
    database.save_game(GameInfo(10, "Game", price="$9.99"))
    database.conn.execute("DELETE FROM price_history")
    database.conn.commit()
    without_history = tmp_path / "without.xlsx"
    database.export_to_excel(without_history)
    workbook = load_workbook(without_history, read_only=True)
    try:
        assert workbook.sheetnames == ["Games", "Reviews"]
    finally:
        workbook.close()

    database.save_game(GameInfo(10, "Game", price="Free"))
    with_history = tmp_path / "with.xlsx"
    database.export_to_excel(with_history)
    workbook = load_workbook(with_history, read_only=True)
    try:
        assert workbook.sheetnames == ["Games", "Reviews", "PriceHistory"]
        rows = list(workbook["PriceHistory"].values)
        assert rows[0] == ("app_id", "game_name", "price", "recorded_at")
        assert [(row[0], row[1], row[2]) for row in rows[1:]] == [
            (10, "Game", "Free")
        ]
    finally:
        workbook.close()


def test_csv_price_history_export_has_expected_columns(
    database: DatabaseManager, tmp_path: Path
) -> None:
    database.save_game(GameInfo(20, "Second", price="$1.99"))
    database.save_game(GameInfo(10, "First", price="$9.99"))
    database.save_game(GameInfo(10, "First", price="Free"))

    database.export_to_csv(tmp_path)

    history = _read_csv(tmp_path / "steam_price_history.csv")
    assert [(row["app_id"], row["game_name"], row["price"]) for row in history] == [
        ("10", "First", "$9.99"),
        ("10", "First", "Free"),
        ("20", "Second", "$1.99"),
    ]
    assert set(history[0]) == {"app_id", "game_name", "price", "recorded_at"}


def test_csv_export_without_history_keeps_two_files(
    database: DatabaseManager, tmp_path: Path
) -> None:
    database.save_game(GameInfo(10, "Game", price=""))
    assert database.get_price_history() == []

    output = tmp_path / "csv"
    database.export_to_csv(output)

    assert sorted(path.name for path in output.iterdir()) == [
        "steam_games.csv",
        "steam_reviews.csv",
    ]


def test_history_csv_is_spreadsheet_safe_and_utf8_sig(
    database: DatabaseManager, tmp_path: Path
) -> None:
    database.save_game(GameInfo(10, "Game", price="=1+1"))

    database.export_to_csv(tmp_path)

    raw = (tmp_path / "steam_price_history.csv").read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf")
    assert _read_csv(tmp_path / "steam_price_history.csv")[0]["price"] == "'=1+1"


@pytest.mark.parametrize("failure_index", [0, 1, 2])
def test_three_csv_files_publish_or_roll_back_together(
    database: DatabaseManager, tmp_path: Path, monkeypatch, failure_index: int
) -> None:
    database.save_game(GameInfo(10, "Game", price="$9.99"))
    targets = [
        tmp_path / "steam_games.csv",
        tmp_path / "steam_reviews.csv",
        tmp_path / "steam_price_history.csv",
    ]
    previous = [b"old games", b"old reviews", b"old history"]
    for target, content in zip(targets, previous, strict=True):
        target.write_bytes(content)

    database.save_game(GameInfo(10, "Game", price="Free"))

    original_replace = os.replace

    def fail_at(source, destination):
        if Path(destination).name == targets[failure_index].name:
            raise OSError("simulated publish failure")
        return original_replace(source, destination)

    monkeypatch.setattr(os, "replace", fail_at)

    with pytest.raises(OSError, match="simulated"):
        database.export_to_csv(tmp_path)

    for target, content in zip(targets, previous, strict=True):
        assert target.read_bytes() == content
    assert list(tmp_path.glob(".steam-csv-*")) == []


def test_three_csv_files_are_all_replaced_on_success(
    database: DatabaseManager, tmp_path: Path
) -> None:
    database.save_game(GameInfo(10, "Game", price="$9.99"))
    database.save_game(GameInfo(10, "Game", price="Free"))
    for name in ("steam_games.csv", "steam_reviews.csv", "steam_price_history.csv"):
        (tmp_path / name).write_bytes(b"old")

    database.export_to_csv(tmp_path)

    history_file = tmp_path / "steam_price_history.csv"
    assert history_file.read_bytes().startswith(b"\xef\xbb\xbf")
    assert _read_csv(tmp_path / "steam_games.csv")[0]["price"] == "Free"
    assert len(_read_csv(tmp_path / "steam_price_history.csv")) == 2


@pytest.mark.parametrize("price, expected", [("$9.99", True), ("", False)])
def test_export_to_csv_reports_whether_history_was_published(
    database: DatabaseManager, tmp_path: Path, price: str, expected: bool
) -> None:
    database.save_game(GameInfo(10, "Game", price=price))

    assert database.export_to_csv(tmp_path) is expected
    assert (tmp_path / "steam_price_history.csv").exists() is expected


def test_two_connections_saving_same_price_keep_one_row(tmp_path: Path) -> None:
    db_path = tmp_path / "shared.db"
    managers = [DatabaseManager(db_path), DatabaseManager(db_path)]
    barrier = threading.Barrier(2)

    def save(manager: DatabaseManager) -> None:
        barrier.wait()
        for _ in range(15):
            manager.save_game(GameInfo(10, "Game", price="$9.99"))

    threads = [threading.Thread(target=save, args=(manager,)) for manager in managers]
    try:
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert _history(managers[0], 10) == [(10, "$9.99")]
    finally:
        for manager in managers:
            manager.close()


def test_deferred_commit_batches_share_one_transaction(tmp_path: Path) -> None:
    database = DatabaseManager(tmp_path / "deferred.db")
    try:
        database.save_game(GameInfo(10, "Game", price="$9.99"), commit=False)
        database.save_game(GameInfo(20, "Other", price="Free"), commit=False)

        assert database.conn.in_transaction
        observer = sqlite3.connect(database.db_path)
        try:
            assert observer.execute("SELECT COUNT(*) FROM games").fetchone()[0] == 0
        finally:
            observer.close()

        database.commit()
        assert _history(database) == [(10, "$9.99"), (20, "Free")]
    finally:
        database.close()


def test_deferred_commit_failure_leaves_previous_batches_uncommitted(
    tmp_path: Path,
) -> None:
    database = DatabaseManager(tmp_path / "deferred.db")
    try:
        database.save_game(GameInfo(10, "Game", price="$9.99"), commit=False)
        database.conn.execute("ALTER TABLE price_history RENAME TO gone")

        with pytest.raises(sqlite3.Error):
            database.save_game(GameInfo(20, "Other", price="Free"), commit=False)
    finally:
        database.close()

    observer = DatabaseManager(tmp_path / "deferred.db")
    try:
        assert observer.conn.execute("SELECT COUNT(*) FROM games").fetchone()[0] == 0
    finally:
        observer.close()


def test_price_history_sheet_keeps_formula_text_as_text(
    database: DatabaseManager, tmp_path: Path
) -> None:
    database.save_game(GameInfo(10, "=1+1", price="=1+1"))

    output = tmp_path / "history.xlsx"
    database.export_to_excel(output)

    workbook = load_workbook(output, read_only=True, data_only=True)
    try:
        rows = list(workbook["PriceHistory"].values)
        exported = dict(zip(rows[0], rows[1], strict=True))
        assert exported["game_name"] == "=1+1"
        assert exported["price"] == "=1+1"
    finally:
        workbook.close()


def test_history_export_keeps_existing_csv_and_sheet_structure(
    database: DatabaseManager, tmp_path: Path
) -> None:
    database.save_game(GameInfo(10, "Game", price="$9.99"))
    database.save_reviews(
        10, [ReviewSnapshot(10, date(2026, 1, 1), 3, 1)]
    )

    database.export_to_csv(tmp_path)
    database.export_to_excel(tmp_path / "out.xlsx")

    assert list(_read_csv(tmp_path / "steam_games.csv")[0]) == [
        "app_id",
        "name",
        "release_date",
        "price",
        "developers",
        "publishers",
        "genres",
        "description",
        "updated_at",
    ]
    assert list(_read_csv(tmp_path / "steam_reviews.csv")[0])[:5] == [
        "id",
        "app_id",
        "date",
        "recommendations_up",
        "recommendations_down",
    ]
    workbook = load_workbook(tmp_path / "out.xlsx", read_only=True)
    try:
        assert workbook.sheetnames == ["Games", "Reviews", "PriceHistory"]
    finally:
        workbook.close()
