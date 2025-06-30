import psycopg2
from psycopg2 import pool

# Create a connection pool (adjust minconn and maxconn as needed)
db_pool = psycopg2.pool.SimpleConnectionPool(
    minconn=1,
    maxconn=5,
    user='postgres',
    password='REMOVED',
    host='192.168.1.106',
    port='5432',
    database='automation_app'
)

def get_db_conn():
    if db_pool:
        return db_pool.getconn()

def put_db_conn(conn):
    if db_pool:
        db_pool.putconn(conn)
