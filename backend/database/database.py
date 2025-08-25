import psycopg2
from psycopg2 import pool
import sys

# Local Imports
sys.path.append('M:/Q_Drive/Projects/RemiHub/')
from backend.config import load_config

# Initialize our config
config = load_config('config/config.ini')['Database']

# Create a connection pool (adjust minconn and maxconn as needed)
db_pool = psycopg2.pool.SimpleConnectionPool(
    minconn=1,
    maxconn=5,
    user=config['user'],
    password=config['password'],
    host=config['host'],
    port=config['port'],
    database=config['database'],
)

def get_db_conn():
    if db_pool:
        return db_pool.getconn()

def put_db_conn(conn):
    if db_pool:
        db_pool.putconn(conn)
