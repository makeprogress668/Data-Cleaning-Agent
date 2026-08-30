import threading
import time
from collections import OrderedDict
from pathlib import Path

import pandas as pd

import data_agent.tools.excel_reader as excel_reader
from data_agent.tools.excel_reader import clear_table_cache, read_tables_from_paths


def test_read_tables_from_paths_expands_every_excel_sheet(tmp_path: Path) -> None:
    workbook = tmp_path / "sample.xlsx"
    with pd.ExcelWriter(workbook, engine="openpyxl") as writer:
        pd.DataFrame({"id": [1, 2]}).to_excel(writer, sheet_name="users", index=False)
        pd.DataFrame({"id": [1], "amount": [10]}).to_excel(
            writer,
            sheet_name="orders",
            index=False,
        )

    tables, inventory = read_tables_from_paths([workbook])

    assert set(tables) == {"sample__users", "sample__orders"}
    assert tables["sample__users"].shape == (2, 1)
    assert set(inventory["sheet"]) == {"users", "orders"}


def test_repeated_reads_are_served_from_cache(tmp_path: Path, monkeypatch) -> None:
    clear_table_cache()
    csv = tmp_path / "orders.csv"
    pd.DataFrame({"id": [1, 2, 3]}).to_csv(csv, index=False)

    # First read populates the cache.
    read_tables_from_paths([csv])

    # A second read of the unchanged file must not touch the pandas parser again.
    calls = {"count": 0}
    original = pd.read_csv

    def _counting_read_csv(*args, **kwargs):
        calls["count"] += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(pd, "read_csv", _counting_read_csv)
    tables, _ = read_tables_from_paths([csv])

    assert calls["count"] == 0
    assert tables["orders"].shape == (3, 1)
    clear_table_cache()


def test_cache_returns_isolated_copies(tmp_path: Path) -> None:
    clear_table_cache()
    csv = tmp_path / "orders.csv"
    pd.DataFrame({"id": [1, 2, 3]}).to_csv(csv, index=False)

    first, _ = read_tables_from_paths([csv])
    first["orders"].loc[0, "id"] = 999  # mutate the returned frame

    second, _ = read_tables_from_paths([csv])
    # The cache must be immune to the caller's mutation.
    assert second["orders"].loc[0, "id"] == 1
    clear_table_cache()


def test_cache_invalidates_when_file_changes(tmp_path: Path) -> None:
    clear_table_cache()
    csv = tmp_path / "orders.csv"
    pd.DataFrame({"id": [1, 2]}).to_csv(csv, index=False)
    first, _ = read_tables_from_paths([csv])
    assert first["orders"].shape == (2, 1)

    # Rewrite with different content/size so the signature changes.
    pd.DataFrame({"id": [1, 2, 3, 4]}).to_csv(csv, index=False)
    second, _ = read_tables_from_paths([csv])
    assert second["orders"].shape == (4, 1)
    clear_table_cache()


def test_cache_is_safe_during_concurrent_eviction(
    tmp_path: Path,
    monkeypatch,
) -> None:
    clear_table_cache()
    first_csv = tmp_path / "first.csv"
    second_csv = tmp_path / "second.csv"
    pd.DataFrame({"id": [1]}).to_csv(first_csv, index=False)
    pd.DataFrame({"id": [2]}).to_csv(second_csv, index=False)
    read_tables_from_paths([first_csv])

    first_key = next(iter(excel_reader._table_cache))
    cache_hit = threading.Event()
    release_hit = threading.Event()

    class SlowHitCache(OrderedDict):
        def get(self, key, default=None):
            value = super().get(key, default)
            if key == first_key and value is not default:
                cache_hit.set()
                release_hit.wait(timeout=2)
            return value

    monkeypatch.setattr(
        excel_reader,
        "_table_cache",
        SlowHitCache(excel_reader._table_cache),
    )
    monkeypatch.setattr(excel_reader, "_TABLE_CACHE_MAX_ENTRIES", 1)

    errors: list[Exception] = []

    def read(path: Path) -> None:
        try:
            read_tables_from_paths([path])
        except Exception as exc:  # pragma: no cover - asserted below
            errors.append(exc)

    first_reader = threading.Thread(target=read, args=(first_csv,))
    second_reader = threading.Thread(target=read, args=(second_csv,))
    first_reader.start()
    assert cache_hit.wait(timeout=2)
    second_reader.start()
    time.sleep(0.05)
    release_hit.set()
    first_reader.join(timeout=2)
    second_reader.join(timeout=2)

    assert not first_reader.is_alive()
    assert not second_reader.is_alive()
    assert errors == []
    clear_table_cache()
