"""Minimal in-memory stand-in for the supabase-py query builder used by SupabaseUserStore."""

from __future__ import annotations

import copy
from types import SimpleNamespace
from typing import Any

from postgrest.exceptions import APIError


class _Query:
    def __init__(self, db: "FakeSupabase", table: str) -> None:
        self.db, self.table_name = db, table
        self.op: str | None = None
        self.payload: dict[str, Any] | None = None
        self.filters: list[tuple[str, Any]] = []
        self.max_rows: int | None = None

    def select(self, _columns: str = "*") -> "_Query":
        self.op = "select"
        return self

    def insert(self, payload: dict[str, Any]) -> "_Query":
        self.op, self.payload = "insert", dict(payload)
        return self

    def eq(self, column: str, value: Any) -> "_Query":
        self.filters.append((column, value))
        return self

    def limit(self, n: int) -> "_Query":
        self.max_rows = n
        return self

    def execute(self) -> SimpleNamespace:
        self.db.calls.append((self.op, self.table_name, self.filters, self.payload))
        if self.db.fail_with is not None:
            raise self.db.fail_with
        rows = self.db.tables.setdefault(self.table_name, [])
        if self.op == "select":
            found = [r for r in rows if all(r.get(c) == v for c, v in self.filters)]
            return SimpleNamespace(data=copy.deepcopy(found[: self.max_rows]))
        if self.op == "insert":
            assert self.payload is not None
            if any(r["email"] == self.payload["email"] for r in rows):
                raise APIError({"code": "23505", "message": "duplicate key value violates unique constraint"})
            row = {"id": f"uuid-{len(rows) + 1}", **self.payload}
            rows.append(row)
            return SimpleNamespace(data=[copy.deepcopy(row)])
        raise AssertionError(f"operación no soportada: {self.op}")


class FakeSupabase:
    def __init__(self) -> None:
        self.tables: dict[str, list[dict[str, Any]]] = {}
        self.calls: list[tuple] = []
        self.fail_with: Exception | None = None

    def table(self, name: str) -> _Query:
        return _Query(self, name)

    def rows(self, table: str = "users") -> list[dict[str, Any]]:
        return self.tables.get(table, [])
