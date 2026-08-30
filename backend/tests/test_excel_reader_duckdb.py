import importlib

import pandas as pd
import pytest

import data_agent.tools.excel_reader as excel_reader
from data_agent.tools.excel_reader import read_table


def test_read_table_reads_csv_with_pandas(tmp_path) -> None:
    path = tmp_path / "small.csv"
    pd.DataFrame({"id": [1, 2], "name": ["a", "b"]}).to_csv(path, index=False)

    frame = read_table(path)

    assert list(frame.columns) == ["id", "name"]
    assert frame.shape == (2, 2)


def test_read_table_uses_duckdb_fast_path_for_large_csv(tmp_path, monkeypatch) -> None:
    pytest.importorskip("duckdb")
    # Force the fast path regardless of file size.
    monkeypatch.setenv("DATA_AGENT_DUCKDB_CSV_MIN_BYTES", "1")
    module = importlib.reload(excel_reader)
    try:
        path = tmp_path / "big.csv"
        pd.DataFrame(
            {"id": [1, 2, 3], "name": ["a", "b", "c"], "amount": [10.5, 20.0, None]}
        ).to_csv(path, index=False)

        frame = module.read_table(path)

        assert list(frame.columns) == ["id", "name", "amount"]
        assert frame.shape == (3, 3)
        # DuckDB infers numeric types just like pandas.
        assert str(frame["id"].dtype) == "int64"
        assert str(frame["amount"].dtype) == "float64"
    finally:
        # Restore default threshold for subsequent tests.
        monkeypatch.delenv("DATA_AGENT_DUCKDB_CSV_MIN_BYTES", raising=False)
        importlib.reload(excel_reader)


def test_duckdb_and_pandas_readers_agree(tmp_path) -> None:
    pytest.importorskip("duckdb")
    path = tmp_path / "data.csv"
    pd.DataFrame({"id": [1, 2], "label": ["x", "y"]}).to_csv(path, index=False)

    from data_agent.tools.excel_reader import _read_csv_with_duckdb, _read_csv_with_pandas

    duck = _read_csv_with_duckdb(path)
    pandas_frame = _read_csv_with_pandas(path)

    assert duck is not None
    pd.testing.assert_frame_equal(
        duck.reset_index(drop=True),
        pandas_frame.reset_index(drop=True),
    )
