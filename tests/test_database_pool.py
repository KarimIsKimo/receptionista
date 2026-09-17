import unittest

import psycopg2

from receptionist.database import DatabasePool


class FakeConnection:
    def __init__(self, *, closed=False):
        self.closed = int(closed)
        self.rollbacks = 0

    def rollback(self):
        self.rollbacks += 1


class FakePool:
    def __init__(self, connections):
        self.connections = list(connections)
        self.put_calls = []
        self.closed = False

    def getconn(self):
        return self.connections.pop(0)

    def putconn(self, connection, close=False):
        self.put_calls.append((connection, close))

    def closeall(self):
        self.closed = True


class DatabasePoolTests(unittest.TestCase):
    def build(self, connections):
        fake = FakePool(connections)
        calls = []

        def factory(minimum, maximum, dsn, **kwargs):
            calls.append((minimum, maximum, dsn, kwargs))
            return fake

        database = DatabasePool(
            "postgresql://example/test",
            min_connections=1,
            max_connections=3,
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


if __name__ == "__main__":
    unittest.main()
