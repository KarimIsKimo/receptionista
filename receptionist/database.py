"""Small, defensive PostgreSQL connection pool for the web application."""

from __future__ import annotations

import threading
from contextlib import contextmanager
from typing import Any, Callable, Iterator

import psycopg2
from psycopg2.pool import ThreadedConnectionPool


class DatabasePool:
    """Lazily initialize and safely recycle a bounded psycopg2 pool."""

    def __init__(
        self,
        dsn: str,
        *,
        min_connections: int = 1,
        max_connections: int = 4,
        pool_factory: Callable[..., Any] = ThreadedConnectionPool,
        connect_kwargs: dict[str, Any] | None = None,
    ):
        self.dsn = dsn
        self.min_connections = max(1, min_connections)
        self.max_connections = max(self.min_connections, max_connections)
        self.pool_factory = pool_factory
        self.connect_kwargs = dict(connect_kwargs or {})
        self._pool: Any | None = None
        self._lock = threading.Lock()

    def start(self) -> Any:
        if self._pool is not None:
            return self._pool
        with self._lock:
            if self._pool is None:
                self._pool = self.pool_factory(
                    self.min_connections,
                    self.max_connections,
                    self.dsn,
                    **self.connect_kwargs,
                )
        return self._pool

    @contextmanager
    def connection(self) -> Iterator[Any]:
        pool = self.start()
        connection = pool.getconn()
        discard = bool(getattr(connection, "closed", False))
        if discard:
            pool.putconn(connection, close=True)
            connection = pool.getconn()
            discard = bool(getattr(connection, "closed", False))
        try:
            if discard:
                raise psycopg2.InterfaceError("PostgreSQL pool returned a closed connection")
            yield connection
        except Exception as exc:
            discard = discard or bool(getattr(connection, "closed", False)) or isinstance(
                exc,
                (psycopg2.InterfaceError, psycopg2.OperationalError),
            )
            if not discard:
                try:
                    connection.rollback()
                except Exception:
                    discard = True
            raise
        finally:
            pool.putconn(
                connection,
                close=discard or bool(getattr(connection, "closed", False)),
            )

    def close(self) -> None:
        with self._lock:
            pool, self._pool = self._pool, None
        if pool is not None:
            pool.closeall()
