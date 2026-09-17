"""Small, defensive PostgreSQL connection pool for the web application."""

from __future__ import annotations

import threading
from contextlib import contextmanager
from typing import Any, Callable, Iterator

import psycopg2
from psycopg2.pool import ThreadedConnectionPool


class DatabasePoolTimeoutError(TimeoutError):
    """Raised when every configured database connection stays busy."""


class DatabasePool:
    """Lazily initialize and safely recycle a bounded psycopg2 pool."""

    def __init__(
        self,
        dsn: str,
        *,
        min_connections: int = 1,
        max_connections: int = 4,
        acquire_timeout: float = 5.0,
        pool_factory: Callable[..., Any] = ThreadedConnectionPool,
        connect_kwargs: dict[str, Any] | None = None,
    ):
        self.dsn = dsn
        self.min_connections = max(1, min_connections)
        self.max_connections = max(self.min_connections, max_connections)
        self.acquire_timeout = max(0.01, float(acquire_timeout))
        self.pool_factory = pool_factory
        self.connect_kwargs = dict(connect_kwargs or {})
        self._pool: Any | None = None
        self._lock = threading.Lock()
        self._capacity = threading.BoundedSemaphore(self.max_connections)
        self._closed = False

    def start(self) -> Any:
        with self._lock:
            if self._closed:
                raise RuntimeError("PostgreSQL connection pool is closed")
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
        acquired = self._capacity.acquire(timeout=self.acquire_timeout)
        if not acquired:
            raise DatabasePoolTimeoutError(
                "PostgreSQL connection pool remained saturated for "
                f"{self.acquire_timeout:g} seconds"
            )

        pool: Any | None = None
        connection: Any | None = None
        discard = False
        try:
            pool = self.start()
            connection = pool.getconn()
            discard = bool(getattr(connection, "closed", False))
            if discard:
                pool.putconn(connection, close=True)
                connection = None
                connection = pool.getconn()
                discard = bool(getattr(connection, "closed", False))
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
            try:
                if pool is not None and connection is not None:
                    try:
                        pool.putconn(
                            connection,
                            close=discard or bool(getattr(connection, "closed", False)),
                        )
                    except Exception:
                        # closeall() owns every connection once shutdown begins.
                        # A late borrower must still release its capacity permit.
                        if not self._closed:
                            raise
            finally:
                self._capacity.release()

    def close(self) -> None:
        with self._lock:
            self._closed = True
            pool, self._pool = self._pool, None
        if pool is not None:
            pool.closeall()
