"""Thin data-access layer over the SQLite connection.

The :class:`Repository` is deliberately generic: services build on it rather
than talking to sqlite directly, and the Streamlit layer holds a single cached
instance. It knows nothing about crops or procurement specifically.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any, Iterable, Sequence

import pandas as pd

from ..database import schema


class Repository:
    """Owns a sqlite connection and offers small query helpers."""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    # -- construction --------------------------------------------------------
    @classmethod
    def create(cls, db_path: str | Path = ":memory:", *, seed: bool = True) -> "Repository":
        """Open (creating if needed), init schema, and optionally seed."""
        conn = schema.connect(db_path)
        schema.init_schema(conn)
        repo = cls(conn)
        if seed and not schema.is_seeded(conn):
            # Imported lazily to avoid a circular import at module load.
            from ..database import seed as seed_module

            seed_module.seed_all(repo)
        return repo

    # -- low-level -----------------------------------------------------------
    def execute(self, sql: str, params: Sequence[Any] = ()) -> sqlite3.Cursor:
        cur = self.conn.execute(sql, params)
        self.conn.commit()
        return cur

    def executemany(self, sql: str, seq_of_params: Iterable[Sequence[Any]]) -> sqlite3.Cursor:
        cur = self.conn.executemany(sql, list(seq_of_params))
        self.conn.commit()
        return cur

    def query(self, sql: str, params: Sequence[Any] = ()) -> list[sqlite3.Row]:
        return self.conn.execute(sql, params).fetchall()

    def query_one(self, sql: str, params: Sequence[Any] = ()) -> sqlite3.Row | None:
        return self.conn.execute(sql, params).fetchone()

    def scalar(self, sql: str, params: Sequence[Any] = ()) -> Any:
        row = self.conn.execute(sql, params).fetchone()
        if row is None:
            return None
        return row[0]

    def df(self, sql: str, params: Sequence[Any] = ()) -> pd.DataFrame:
        """Run a query and return a pandas DataFrame."""
        return pd.read_sql_query(sql, self.conn, params=params)

    # -- generic insert ------------------------------------------------------
    def insert(self, table: str, values: dict[str, Any], *, or_ignore: bool = False) -> int:
        cols = ", ".join(values.keys())
        placeholders = ", ".join("?" for _ in values)
        verb = "INSERT OR IGNORE" if or_ignore else "INSERT"
        sql = f"{verb} INTO {table} ({cols}) VALUES ({placeholders})"
        cur = self.execute(sql, list(values.values()))
        return int(cur.lastrowid or 0)

    def upsert(self, table: str, values: dict[str, Any], conflict_cols: Sequence[str],
               update_cols: Sequence[str]) -> None:
        cols = ", ".join(values.keys())
        placeholders = ", ".join("?" for _ in values)
        conflict = ", ".join(conflict_cols)
        updates = ", ".join(f"{c}=excluded.{c}" for c in update_cols)
        sql = (
            f"INSERT INTO {table} ({cols}) VALUES ({placeholders}) "
            f"ON CONFLICT ({conflict}) DO UPDATE SET {updates}"
        )
        self.execute(sql, list(values.values()))

    # -- convenience ---------------------------------------------------------
    def table_count(self, table: str) -> int:
        return int(self.scalar(f"SELECT COUNT(*) FROM {table}") or 0)

    def crop_id_by_name(self, name: str) -> int | None:
        row = self.query_one(
            "SELECT id FROM crops WHERE canonical_name = ? COLLATE NOCASE", (name,)
        )
        return int(row["id"]) if row else None

    def close(self) -> None:
        self.conn.close()
