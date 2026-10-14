"""
Connexion à la base de données.

- Si la variable d'environnement DATABASE_URL existe (Neon, Supabase...),
  les données sont stockées dans PostgreSQL : elles survivent aux
  redémarrages et aux redéploiements de Render.
- Sinon, on utilise un simple fichier SQLite local (pratique pour tester
  sur son ordinateur, mais ce fichier est effacé sur Render gratuit).

Le reste du code écrit ses requêtes une seule fois, avec des "?" comme
paramètres : ce fichier s'occupe d'adapter pour PostgreSQL.
"""

import os
import sqlite3

DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()
USE_PG = DATABASE_URL.startswith(("postgres://", "postgresql://"))
SQLITE_PATH = os.environ.get("SQLITE_PATH", "vtt_entretien_v3.db")

if USE_PG:
    import psycopg


class Row(tuple):
    """Ligne de résultat accessible par position (row[0]) et par nom (row["nom"])."""

    def __new__(cls, cols, values):
        row = super().__new__(cls, values)
        row._cols = tuple(cols)
        return row

    def __getitem__(self, key):
        if isinstance(key, str):
            return super().__getitem__(self._cols.index(key))
        return super().__getitem__(key)

    def keys(self):
        return list(self._cols)


def _row_factory(cursor):
    cols = [c.name for c in (cursor.description or [])]

    def make(values):
        return Row(cols, values)

    return make


class _Cursor:
    def __init__(self, cursor):
        self._cur = cursor

    def execute(self, sql, params=()):
        if USE_PG:
            sql = sql.replace("?", "%s")
        self._cur.execute(sql, params)
        return self

    def executemany(self, sql, seq):
        if USE_PG:
            sql = sql.replace("?", "%s")
        self._cur.executemany(sql, seq)
        return self

    def fetchone(self):
        return self._cur.fetchone()

    def fetchall(self):
        return self._cur.fetchall()


class _Connection:
    def __init__(self, conn):
        self._conn = conn

    def cursor(self):
        return _Cursor(self._conn.cursor())

    def commit(self):
        self._conn.commit()

    def rollback(self):
        self._conn.rollback()

    def close(self):
        self._conn.close()


def connect():
    if USE_PG:
        conn = psycopg.connect(
            DATABASE_URL,
            row_factory=_row_factory,
            # Compatible avec les adresses "pooled" de Neon.
            prepare_threshold=None,
        )
        return _Connection(conn)

    conn = sqlite3.connect(SQLITE_PATH)
    conn.row_factory = sqlite3.Row
    return _Connection(conn)


# Types de colonnes qui changent d'un moteur à l'autre.
AUTO_ID = "SERIAL PRIMARY KEY" if USE_PG else "INTEGER PRIMARY KEY AUTOINCREMENT"
