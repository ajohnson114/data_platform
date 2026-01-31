import os
from dagster_duckdb_pandas import duckdb_pandas_io_manager


DUCKDB_PATH = os.environ.get("IO_MANAGER_PATH", "/duckdb_io_manager/my_data.duckdb")

duckdb_io_manager = duckdb_pandas_io_manager.configured({
    "database": DUCKDB_PATH
})

# I want all of the code locations to use the same io manager
global_resources = {'duckdb_io_manager': duckdb_io_manager}