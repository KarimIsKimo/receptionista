import threading
import time
import unittest

import psycopg2

from receptionist.database import DatabasePool, DatabasePoolTimeoutError


class FakeConnection:
    def __init__(self, *, closed=False):
        self.closed = int(closed)
        self.rollbacks = 0

    def rollback(self):
        self.rollbacks += 1


class FakePool:
    def __init__(self, connections, *, get_failures=0):
        self.connections = list(connections)
        self.put_calls = []
        self.closed = False
        self.get_failures = get_failures
        self.lock = threading.Lock()

    def getconn(self):
        with self.lock:
            if self.get_failures:
                self.get_failures -= 1
                raise psycopg2.pool.PoolError("temporary pool failure")
            return self.connections.pop(0)

    def putconn(self, connection, close=False):
        with self.lock:
            self.put_calls.append((connection, close))
            if not close:
                self.connections.append(connection)

    def closeall(self):
        self.closed = True


class DatabasePoolTests(unittest.TestCase):
    def build(
        self,
        connections,
        *,
        max_connections=3,
        acquire_timeout=0.5,
        get_failures=0,
    ):
        fake = FakePool(connections, get_failures=get_failures)
        calls = []

        def factory(minimum, maximum, dsn, **kwargs):
            calls.append((minimum, maximum, dsn, kwargs))
            return fake

        database = DatabasePool(
            "postgresql://example/test",
            min_connections=1,
            max_connections=max_connections,
            acquire_timeout=acquire_timeout,
            pool_factory=factory,
            connect_kwargs={"sslmode": "require"},
        )
        return database, fake, calls

    def test_success_always_returns_connection(self):
        connection = FakeConnection()
        database, pool, calls = self.build([connection])

        with database.connection() as borrowed:
            self.assertIs(borrowed, connection)

        self.assertEqual(pool.put_calls, [(connection, False)])
        self.assertEqual(calls[0][:3], (1, 3, "postgresql://example/test"))
        self.assertEqual(calls[0][3], {"sslmode": "require"})

    def test_application_error_rolls_back_and_returns_connection(self):
        connection = FakeConnection()
        database, pool, _ = self.build([connection])

        with self.assertRaises(ValueError):
            with database.connection():
                raise ValueError("bad query")

        self.assertEqual(connection.rollbacks, 1)
        self.assertEqual(pool.put_calls, [(connection, False)])

    def test_broken_connection_is_discarded_after_driver_error(self):
        connection = FakeConnection()
        database, pool, _ = self.build([connection])

        with self.assertRaises(psycopg2.OperationalError):
            with database.connection():
                raise psycopg2.OperationalError("connection lost")

        self.assertEqual(pool.put_calls, [(connection, True)])
        self.assertEqual(connection.rollbacks, 0)

    def test_closed_connection_is_replaced_without_leaking(self):
        stale = FakeConnection(closed=True)
        healthy = FakeConnection()
        database, pool, _ = self.build([stale, healthy])

        with database.connection() as borrowed:
            self.assertIs(borrowed, healthy)

        self.assertEqual(pool.put_calls, [(stale, True), (healthy, False)])

    def test_shutdown_closes_and_allows_clean_lazy_restart(self):
        first = FakeConnection()
        database, pool, calls = self.build([first])
        database.start()
        database.start()
        self.assertEqual(len(calls), 1)

        database.close()

        self.assertTrue(pool.closed)

    def test_saturated_pool_waits_until_connection_is_returned(self):
        connection = FakeConnection()
        database, pool, _ = self.build(
            [connection], max_connections=1, acquire_timeout=1.0
        )
        first_borrowed = threading.Event()
        release_first = threading.Event()
        second_borrowed = threading.Event()

        def first_worker():
            with database.connection():
                first_borrowed.set()
                release_first.wait(1.0)

        def second_worker():
            with database.connection():
                second_borrowed.set()

        first = threading.Thread(target=first_worker)
        second = threading.Thread(target=second_worker)
        first.start()
        self.assertTrue(first_borrowed.wait(0.5))
        second.start()
        self.assertFalse(second_borrowed.wait(0.05))
        release_first.set()
        first.join(1.0)
        second.join(1.0)

        self.assertTrue(second_borrowed.is_set())
        self.assertFalse(first.is_alive())
        self.assertFalse(second.is_alive())
        self.assertEqual(pool.put_calls, [(connection, False), (connection, False)])

    def test_saturation_timeout_is_controlled_and_does_not_oversubscribe(self):
        connection = FakeConnection()
        database, pool, _ = self.build(
            [connection], max_connections=1, acquire_timeout=0.05
        )

        with database.connection():
            started = time.monotonic()
            with self.assertRaisesRegex(
                DatabasePoolTimeoutError, "remained saturated"
            ):
                with database.connection():
                    pass
            elapsed = time.monotonic() - started

        self.assertGreaterEqual(elapsed, 0.04)
        self.assertEqual(pool.put_calls, [(connection, False)])

    def test_acquisition_and_context_exceptions_do_not_leak_capacity(self):
        connection = FakeConnection()
        database, pool, _ = self.build(
            [connection],
            max_connections=1,
            acquire_timeout=0.05,
            get_failures=1,
        )

        with self.assertRaises(psycopg2.pool.PoolError):
            with database.connection():
                pass

        with self.assertRaises(ValueError):
            with database.connection():
                raise ValueError("application failure")

        with database.connection() as borrowed:
            self.assertIs(borrowed, connection)

        self.assertEqual(connection.rollbacks, 1)
        self.assertEqual(pool.put_calls, [(connection, False), (connection, False)])

    def test_shutdown_rejects_new_borrowers_without_leaking_permits(self):
        database, pool, _ = self.build(
            [FakeConnection()], max_connections=1, acquire_timeout=0.05
        )
        database.start()
        database.close()

        with self.assertRaisesRegex(RuntimeError, "pool is closed"):
            with database.connection():
                pass
        with self.assertRaisesRegex(RuntimeError, "pool is closed"):
            with database.connection():
                pass

        self.assertTrue(pool.closed)


if __name__ == "__main__":
    unittest.main()
