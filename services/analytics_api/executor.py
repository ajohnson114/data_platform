import duckdb
from config import DUCKDB_PATH, CRYPTO_TABLE


def execute_sql(sql):
    con = duckdb.connect(DUCKDB_PATH, read_only=True)
    con.execute("SET search_path = public")
    cursor = con.execute(sql)
    result = cursor.fetchall()
    columns = [desc[0] for desc in cursor.description]
    con.close()
    return columns, result


def get_max_timestamp():
    con = duckdb.connect(DUCKDB_PATH, read_only=True)
    try:
        con.execute("SET search_path = public")
        result = con.execute(
            f"SELECT MAX(event_timestamp) FROM {CRYPTO_TABLE}"
        ).fetchone()[0]
    except Exception:
        result = None
    con.close()
    return result
