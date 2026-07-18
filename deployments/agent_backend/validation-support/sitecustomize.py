"""Prevent deployment validation imports from opening any database connection."""
from psycopg2 import pool


class OfflineThreadedConnectionPool:
    def __init__(self, *args, **kwargs):
        self.args = args
        self.kwargs = kwargs

    def getconn(self):
        raise RuntimeError("database access is disabled during backend validation")

    def putconn(self, _connection):
        return None

    def closeall(self):
        return None


pool.ThreadedConnectionPool = OfflineThreadedConnectionPool
