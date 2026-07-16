from pathlib import Path

from psycopg2 import pool

from backend.config import load_config, resolve_database_config_path


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DATABASE_CONFIG = PROJECT_ROOT / "config" / "config.ini"
database_config_path = resolve_database_config_path(DEFAULT_DATABASE_CONFIG)
config = load_config(str(database_config_path))["Database"]

db_pool = pool.SimpleConnectionPool(
    minconn=1,
    maxconn=5,
    user=config["user"],
    password=config["password"],
    host=config["host"],
    port=config["port"],
    database=config["database"],
)


def get_db_conn():
    if db_pool:
        return db_pool.getconn()


def put_db_conn(conn):
    if db_pool:
        db_pool.putconn(conn)
