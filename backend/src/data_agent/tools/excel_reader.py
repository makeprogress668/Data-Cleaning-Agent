from __future__ import annotations

import logging
import os
import re
import threading
from collections import OrderedDict
from pathlib import Path
from typing import Optional, Union

import pandas as pd

from data_agent.tools.header_detection import reframe_with_header

logger = logging.getLogger(__name__)

SUPPORTED_EXTENSIONS = {".csv", ".xlsx", ".xlsm", ".xls"}

# pandas' own renaming of a duplicate column: 金额 -> 金额.1
_DUPLICATE_SUFFIX = re.compile(r"^.+\.\d+$")

# CSV files above this size use the DuckDB-accelerated reader (faster parsing and
# more robust type/encoding inference on large files). Override via env var.
DUCKDB_CSV_THRESHOLD_BYTES = int(os.environ.get("DATA_AGENT_DUCKDB_CSV_MIN_BYTES", 5_000_000))

# Bounded in-process cache for parsed table sets. A single answer/delivery request
# reads the same uploaded files several times (raw profile, reflection validation,
# and each execute/rerun), and parsing CSV/Excel is the dominant cost. Cache the
# parsed result keyed by the exact file signature (path + mtime + size) so repeated
# reads within a request are served from memory. Cached frames are copied on the way
# out so callers that mutate their tables never corrupt the cache.
_TABLE_CACHE_MAX_ENTRIES = int(os.environ.get("DATA_AGENT_TABLE_CACHE_ENTRIES", 8))
_table_cache: OrderedDict[tuple, tuple[dict[str, pd.DataFrame], pd.DataFrame]] = OrderedDict()
_table_cache_lock = threading.RLock()


def clear_table_cache() -> None:
    """Drop all cached table sets (useful for tests and long-running processes)."""
    with _table_cache_lock:
        _table_cache.clear()


def read_table(path: Union[str, Path], sheet: Optional[Union[str, int]] = None) -> pd.DataFrame:
    file_path = Path(path)
    if not file_path.exists():
        raise FileNotFoundError(f"Input file not found: {file_path}")

    suffix = file_path.suffix.lower()
    if suffix not in SUPPORTED_EXTENSIONS:
        raise ValueError(f"Unsupported input file type: {suffix}")

    if suffix == ".csv":
        return _read_csv(file_path)

    sheet_name = 0 if sheet is None else sheet
    return _read_excel_sheet(file_path, sheet_name)


def _read_excel_sheet(file_path: Path, sheet_name: Union[str, int]) -> pd.DataFrame:
    """Read one sheet, locating the header when it is not the first row.

    A title line or a two-level merged header is normal in a business workbook and
    fatal to the default reading: columns come back as 订单信息/订单信息.1 and the row
    holding the real names becomes data. Detection is deliberately conservative and
    returns ``None`` for ordinary files, which keep the original single read.
    """

    frame = pd.read_excel(file_path, sheet_name=sheet_name)
    if not _needs_header_detection(frame):
        return frame
    raw = pd.read_excel(file_path, sheet_name=sheet_name, header=None)
    reframed = reframe_with_header(raw)
    if reframed is None:
        return frame
    logger.info(
        "识别到非首行表头：%s -> %s",
        file_path.name,
        list(reframed.columns)[:8],
    )
    return reframed


def _needs_header_detection(frame: pd.DataFrame) -> bool:
    """Cheap check so well-formed files never pay for a second read.

    pandas renames duplicate headers to ``金额.1`` and unnamed ones to ``Unnamed: 3``.
    Either is a sign the first row was not the header — no one names two columns the
    same thing, and a merged cell leaves the columns beside it nameless.
    """

    names = [str(column) for column in frame.columns]
    return any(
        name.startswith("Unnamed:") or _DUPLICATE_SUFFIX.match(name) for name in names
    )


def _read_csv(file_path: Path) -> pd.DataFrame:
    """Read a CSV, using the DuckDB fast path for large files with pandas fallback."""
    try:
        if file_path.stat().st_size >= DUCKDB_CSV_THRESHOLD_BYTES:
            frame = _read_csv_with_duckdb(file_path)
            if frame is not None:
                return frame
    except OSError:
        pass
    return _read_csv_with_pandas(file_path)


def _read_csv_with_duckdb(file_path: Path) -> pd.DataFrame | None:
    """Parse a large CSV via DuckDB; return ``None`` to signal pandas fallback.

    DuckDB's ``read_csv_auto`` handles delimiter/type/encoding sniffing efficiently
    on large inputs. Any failure (missing dependency, parse issue) falls back to the
    battle-tested pandas path so behaviour never regresses.
    """
    try:
        import duckdb
    except ImportError:
        return None
    try:
        connection = duckdb.connect()
        try:
            query = "SELECT * FROM read_csv_auto(?, header=true, all_varchar=false)"
            frame = connection.execute(query, [str(file_path)]).fetch_df()
        finally:
            connection.close()
        logger.info("使用 DuckDB 读取大文件 CSV: %s (%s 行)", file_path.name, len(frame))
        return frame
    except Exception as exc:  # noqa: BLE001 - fall back to pandas on any DuckDB issue
        logger.warning("DuckDB 读取失败，回退 pandas: %s (%s)", file_path.name, exc)
        return None


def _read_csv_with_pandas(file_path: Path) -> pd.DataFrame:
    for encoding in ("utf-8-sig", "utf-8", "gb18030"):
        try:
            return pd.read_csv(file_path, encoding=encoding)
        except UnicodeDecodeError:
            continue
    return pd.read_csv(file_path)


def discover_input_files(paths: list[Union[str, Path]], recursive: bool = True) -> list[Path]:
    files: list[Path] = []
    for raw_path in paths:
        path = Path(raw_path)
        if not path.exists():
            raise FileNotFoundError(f"Input path not found: {path}")
        if path.is_file():
            if _is_supported_data_file(path):
                files.append(path)
            continue

        iterator = path.rglob("*") if recursive else path.glob("*")
        files.extend(file for file in iterator if _is_supported_data_file(file))

    return _deduplicate_equivalent_files(sorted(dict.fromkeys(files)))


def read_tables_from_paths(
    paths: list[Union[str, Path]],
    recursive: bool = True,
) -> tuple[dict[str, pd.DataFrame], pd.DataFrame]:
    """Read CSV files and every sheet from Excel workbooks into named tables.

    Results are cached per request by the exact file signature (path + mtime + size),
    so the repeated reads a single delivery performs (profiling, reflection
    validation, and each execute/rerun) parse the files only once. Callers receive
    fresh copies, keeping the cache immune to downstream mutation.
    """

    discovered = discover_input_files(paths, recursive=recursive)
    cache_key = tuple(_file_signature(file_path) for file_path in discovered)

    with _table_cache_lock:
        cached = _table_cache.get(cache_key)
        if cached is not None:
            _table_cache.move_to_end(cache_key)
            tables, inventory = cached
            return (
                {name: frame.copy() for name, frame in tables.items()},
                inventory.copy(),
            )

    tables: dict[str, pd.DataFrame] = {}
    inventory_rows = []

    for file_path in discovered:
        suffix = file_path.suffix.lower()
        if suffix == ".csv":
            table_name = _unique_table_name(tables, file_path.stem)
            tables[table_name] = read_table(file_path)
            inventory_rows.append(_inventory_row(table_name, file_path, sheet=None))
            continue

        workbook = pd.ExcelFile(file_path)
        single_sheet = len(workbook.sheet_names) == 1
        for sheet_name in workbook.sheet_names:
            base_name = file_path.stem if single_sheet else f"{file_path.stem}__{sheet_name}"
            table_name = _unique_table_name(tables, base_name)
            tables[table_name] = _read_excel_sheet(file_path, sheet_name)
            inventory_rows.append(_inventory_row(table_name, file_path, sheet=sheet_name))

    inventory = pd.DataFrame(inventory_rows)
    _store_in_cache(cache_key, tables, inventory)
    # Return copies so the caller can mutate freely without touching the cached set.
    return {name: frame.copy() for name, frame in tables.items()}, inventory.copy()


def _file_signature(file_path: Path) -> tuple:
    try:
        stat = file_path.stat()
        return (str(file_path), int(stat.st_mtime_ns), int(stat.st_size))
    except OSError:
        # If the file is unstat-able, use a sentinel so it never collides with a
        # real signature (forces a fresh read and skips caching benefits safely).
        return (str(file_path), None, None)


def _store_in_cache(
    cache_key: tuple,
    tables: dict[str, pd.DataFrame],
    inventory: pd.DataFrame,
) -> None:
    if not cache_key:
        return
    # Store defensive copies so later mutations by callers cannot corrupt the cache.
    with _table_cache_lock:
        _table_cache[cache_key] = (
            {name: frame.copy() for name, frame in tables.items()},
            inventory.copy(),
        )
        _table_cache.move_to_end(cache_key)
        while len(_table_cache) > _TABLE_CACHE_MAX_ENTRIES:
            _table_cache.popitem(last=False)


def _is_supported_data_file(path: Path) -> bool:
    if not path.is_file():
        return False
    if path.name.startswith("~$"):
        return False
    return path.suffix.lower() in SUPPORTED_EXTENSIONS


def _deduplicate_equivalent_files(files: list[Path]) -> list[Path]:
    by_stem: dict[tuple[Path, str], list[Path]] = {}
    for file in files:
        by_stem.setdefault((file.parent, file.stem), []).append(file)

    selected = []
    for group in by_stem.values():
        excel_files = [file for file in group if file.suffix.lower() in {".xlsx", ".xlsm", ".xls"}]
        selected.extend(excel_files or group)

    return sorted(selected)


def _unique_table_name(tables: dict[str, pd.DataFrame], base_name: str) -> str:
    table_name = base_name
    counter = 2
    while table_name in tables:
        table_name = f"{base_name}__{counter}"
        counter += 1
    return table_name


def _inventory_row(table_name: str, file_path: Path, sheet: str | None) -> dict[str, object]:
    return {
        "table": table_name,
        "file": str(file_path),
        "file_name": file_path.name,
        "sheet": sheet or "",
        "file_type": file_path.suffix.lower().lstrip("."),
    }
