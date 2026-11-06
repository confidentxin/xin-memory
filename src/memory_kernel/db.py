from __future__ import annotations

import psycopg
from psycopg.rows import dict_row


def connect(database_url: str) -> psycopg.Connection:
    return psycopg.connect(database_url, row_factory=dict_row)


def apply_sql_script(conn: psycopg.Connection, sql: str) -> None:
    statements: list[str] = []
    buffer: list[str] = []
    for line in sql.splitlines():
        stripped = line.strip()
        if not buffer and stripped.startswith("--"):
            continue
        buffer.append(line)
        if stripped.endswith(";"):
            statement = "\n".join(buffer).strip()
            if statement.endswith(";"):
                statement = statement[:-1].strip()
            if statement:
                statements.append(statement)
            buffer = []
    trailing = "\n".join(buffer).strip().rstrip(";").strip()
    if trailing:
        statements.append(trailing)
    for statement in statements:
        conn.execute(statement)
