# shared/resources/clickhouse_resource.py
from contextlib import contextmanager

import clickhouse_connect
import numpy as np
import pandas as pd

# pandas dtype -> ClickHouse column type. Everything is Nullable so NaN/NaT columns
# coming out of pandas don't get rejected on insert.
#
# The capitalised keys are pandas' nullable extension dtypes, which read_sql hands
# back for nullable Postgres columns. They are distinct dtypes from the numpy ones
# and str() renders them capitalised, so both spellings have to be here.
DTYPE_TO_CLICKHOUSE = {
    "int64": "Int64",
    "int32": "Int32",
    "int16": "Int16",
    "int8": "Int8",
    "float64": "Float64",
    "float32": "Float32",
    "bool": "Bool",
    "Int64": "Int64",
    "Int32": "Int32",
    "Int16": "Int16",
    "Int8": "Int8",
    "Float64": "Float64",
    "Float32": "Float32",
    "boolean": "Bool",
}


def clickhouse_type(dtype, nullable: bool = True) -> str:
    """
    Map a pandas dtype to a ClickHouse type, defaulting to String.

    `nullable=False` is for the sorting key: ClickHouse rejects a Nullable column
    as an ORDER BY key unless allow_nullable_key is set, and the incremental
    high-water mark column is never null anyway.
    """
    name = str(dtype)

    # Match datetimes on the family rather than an exact dtype string. pandas
    # carries a resolution in the dtype (datetime64[ns], [us], [ms], [s]) and picks
    # it based on the source data and the pandas version — pandas 3 infers [us]
    # where pandas 2 gave [ns]. An unrecognised name falls through to String below,
    # and the insert then dies with "'Timestamp' object has no attribute 'encode'",
    # so an exact-match list here is a silent trap. tz-aware dtypes carry the zone
    # after a comma: datetime64[us, UTC].
    if name.startswith("datetime64["):
        base = "DateTime64(3, 'UTC')" if "," in name else "DateTime64(3)"
    else:
        base = DTYPE_TO_CLICKHOUSE.get(name, "String")

    return f"Nullable({base})" if nullable else base


class ClickHouseResource:
    """
    The analytical warehouse. Assets write to it explicitly, one table per asset.
    Accepts credentials directly as keyword arguments.
    """

    def __init__(
        self,
        username: str,
        password: str,
        host: str = "clickhouse",
        port: int = 8123,
        database: str = "analytics",
    ):
        self.username = username
        self.password = password
        self.host = host
        self.port = port
        self.database = database

    @contextmanager
    def session(self, use_database: bool = True):
        """
        A client with guaranteed cleanup, for callers that drive their own SQL.

        The direct loader builds statements this class knows nothing about --
        table functions over Parquet, DESCRIBE against a file -- so it needs the
        connection without the pandas helpers wrapped around it. Everything
        inside this class opens and closes its own client the same way; this just
        makes that available outside it rather than exposing _get_client.
        """
        client = self._get_client(self.database if use_database else None)
        try:
            yield client
        finally:
            client.close()

    def _get_client(self, database: str | None = None):
        """
        Connect to the warehouse, optionally with a default database for the session.

        Pass nothing when the database might not exist yet. clickhouse_connect
        validates the database as part of connecting, so asking for `analytics`
        before anything has created it fails with UNKNOWN_DATABASE — before the
        CREATE DATABASE statement that would have fixed it ever gets sent.
        """
        return clickhouse_connect.get_client(
            host=self.host,
            port=self.port,
            username=self.username,
            password=self.password,
            **({"database": database} if database else {}),
        )

    @staticmethod
    def _array_columns(df: pd.DataFrame) -> list:
        """
        Columns holding list-like values rather than scalars.

        A Postgres text[] survives read_sql and Parquet as an object column full
        of numpy arrays, and pandas reports its dtype as plain `object` — the
        same dtype a column of strings has. Only the values distinguish them, so
        this samples one. Getting it wrong is not a loud failure: the column maps
        to String and the insert dies inside the driver with
        "'numpy.ndarray' object has no attribute 'encode'".
        """
        found = []
        for col in df.columns:
            if df[col].dtype != object:
                continue
            sample = next((v for v in df[col] if isinstance(v, (list, tuple, np.ndarray))), None)
            if sample is not None:
                found.append(col)
        return found

    @staticmethod
    def _normalise_arrays(df: pd.DataFrame, columns) -> pd.DataFrame:
        """
        Coerce array columns so ClickHouse will accept them.

        ClickHouse has no Nullable(Array(...)) — it rejects the type outright —
        so a missing list has to become an empty one rather than a null.
        """
        if not columns:
            return df
        df = df.copy()
        for col in columns:
            df[col] = [
                [str(item) for item in value] if isinstance(value, (list, tuple, np.ndarray)) else []
                for value in df[col]
            ]
        return df

    def _column_ddl(self, df: pd.DataFrame, non_nullable=frozenset(), overrides=None) -> str:
        overrides = overrides or {}
        return ", ".join(
            f"`{col}` {overrides.get(col) or clickhouse_type(dtype, nullable=col not in non_nullable)}"
            for col, dtype in df.dtypes.items()
        )

    def _add_missing_columns(self, client, name: str, df: pd.DataFrame) -> list:
        """
        Widen an existing table to cover any column the DataFrame has and it doesn't.

        Appending can't rebuild the table from the current frame the way the
        staged full replace does, so schema drift becomes an explicit, logged
        ALTER instead of an implicit side effect of rewriting everything.
        Columns that disappear from the source are left in place and simply go
        null for new rows.
        """
        existing = {
            row[0]
            for row in client.query(
                "SELECT name FROM system.columns WHERE database = {db:String} AND table = {tbl:String}",
                parameters={"db": self.database, "tbl": name},
            ).result_rows
        }

        array_columns = set(self._array_columns(df))
        added = []
        for col, dtype in df.dtypes.items():
            if col not in existing:
                col_type = "Array(String)" if col in array_columns else clickhouse_type(dtype)
                client.command(
                    f"ALTER TABLE `{self.database}`.`{name}` ADD COLUMN `{col}` {col_type}"
                )
                added.append(col)
        return added

    def write_table(self, name: str, df: pd.DataFrame) -> None:
        """
        Atomically replace the whole table with the contents of df.

        The right tool when the source is mutable — rows can be updated or
        deleted in place, so the only way to be correct is to re-read it. Both
        current source tables are append-only and use append_table instead;
        full replace re-reads everything on every run, which is O(table) per
        materialization no matter how little changed.
        """
        staging = f"{name}__staging"
        array_columns = self._array_columns(df)
        df = self._normalise_arrays(df, array_columns)
        columns = self._column_ddl(df, overrides={c: "Array(String)" for c in array_columns})

        # No session database: this is the call that bootstraps it on a fresh
        # warehouse. Every statement below names the database explicitly.
        client = self._get_client()
        try:
            client.command(f"CREATE DATABASE IF NOT EXISTS `{self.database}`")

            # Load into a staging table and EXCHANGE it with the live one. The exchange is
            # atomic, so readers never see a missing or half-populated table, and because
            # staging is built from the current DataFrame the live table picks up any schema
            # drift instead of failing the insert. The live table is created first (as a
            # no-op if it already exists) because EXCHANGE needs both sides to exist.
            client.command(
                f"CREATE TABLE IF NOT EXISTS `{self.database}`.`{name}` ({columns}) "
                f"ENGINE = MergeTree ORDER BY tuple()"
            )
            client.command(f"DROP TABLE IF EXISTS `{self.database}`.`{staging}`")
            client.command(
                f"CREATE TABLE `{self.database}`.`{staging}` ({columns}) "
                f"ENGINE = MergeTree ORDER BY tuple()"
            )

            client.insert_df(table=staging, df=df, database=self.database)

            client.command(
                f"EXCHANGE TABLES `{self.database}`.`{name}` AND `{self.database}`.`{staging}`"
            )
            client.command(f"DROP TABLE IF EXISTS `{self.database}`.`{staging}`")
        finally:
            client.close()

    def append_table(
        self,
        name: str,
        df: pd.DataFrame,
        order_by,
        version_column: str = None,
        is_deleted_column: str = None,
        partition_by: str = None,
    ) -> None:
        """
        Append rows to a warehouse table, creating it if it doesn't exist.

        `order_by` is the ReplacingMergeTree sorting key -- a column name, or a
        sequence of them for a composite key. It is also the deduplication key,
        so re-delivering a row that is already present collapses rather than
        double-counting. That is what makes a retried or overlapping read safe.

        Passing `version_column` makes the highest version win for a key instead
        of an arbitrary row, which turns an append-only stream of change events
        into current state: an update is simply a later version of the same key.
        Adding `is_deleted_column` (which ClickHouse requires to be UInt8) marks
        tombstones, and FINAL then hides those keys entirely.

        Deduplication happens at merge time, so a reader can still observe a
        duplicate -- or a deleted row -- in the window between an append and the
        background merge. Readers that need exactness ask for it: `SELECT ...
        FINAL`, which the ml pipeline's query does, and which the analytics_ro
        profile applies automatically for the NL-to-SQL service. Note FINAL only
        *hides* deleted rows; the bytes stay on disk until they are purged.
        """
        if df.empty:
            return

        if is_deleted_column and not version_column:
            raise ValueError("is_deleted_column requires version_column: ClickHouse needs a "
                             "version to know which row for a key is the current one")

        keys = [order_by] if isinstance(order_by, str) else list(order_by)
        order_by_sql = ", ".join(f"`{k}`" for k in keys)

        # The sorting key, the version and the delete flag can never be Nullable:
        # ClickHouse rejects a nullable sorting key outright, and is_deleted must
        # be exactly UInt8 rather than whatever the dataframe's int dtype maps to.
        # Array(String) rather than Nullable(String): see _array_columns.
        array_columns = self._array_columns(df)
        df = self._normalise_arrays(df, array_columns)

        non_nullable = set(keys)
        overrides = {c: "Array(String)" for c in array_columns}
        if version_column:
            non_nullable.add(version_column)
        if is_deleted_column:
            non_nullable.add(is_deleted_column)
            overrides[is_deleted_column] = "UInt8"

        if version_column and is_deleted_column:
            engine = f"ReplacingMergeTree(`{version_column}`, `{is_deleted_column}`)"
        elif version_column:
            engine = f"ReplacingMergeTree(`{version_column}`)"
        else:
            engine = "ReplacingMergeTree"

        client = self._get_client()
        try:
            client.command(f"CREATE DATABASE IF NOT EXISTS `{self.database}`")
            # Kept identical to the direct loader's DDL, because the two paths
            # must produce the same table -- a frame-loaded table that was not
            # partitioned would make purge_deleted's per-partition delete
            # degenerate back into a whole-table rewrite.
            partition_clause = f"PARTITION BY {partition_by} " if partition_by else ""
            client.command(
                f"CREATE TABLE IF NOT EXISTS `{self.database}`.`{name}` "
                f"({self._column_ddl(df, non_nullable=non_nullable, overrides=overrides)}) "
                f"ENGINE = {engine} {partition_clause}ORDER BY ({order_by_sql})"
            )
            self._add_missing_columns(client, name, df)
            client.insert_df(table=name, df=df, database=self.database)
        finally:
            client.close()

    def purge_deleted(self, name: str, order_by, version_column: str,
                      is_deleted_column: str = "is_deleted") -> int:
        """
        Physically remove every row belonging to a key whose CURRENT state is
        deleted.

        FINAL hides deleted keys from readers, but the rows are still on disk.
        For retracted user content that is not good enough -- "deleted" has to
        mean the bytes are gone -- so this runs on a schedule and does the real
        removal, taking the tombstone and the record it retracts together.

        Implemented as an ALTER ... DELETE mutation rather than OPTIMIZE ... FINAL
        CLEANUP: the CLEANUP form is gated behind an experimental table setting in
        24.8 and refuses to run, whereas the mutation is supported and idempotent.
        Mutations rewrite the parts they touch, which is why this is scheduled
        rather than run on every load. Returns how many keys were purged.

        REWRITTEN AFTER AN OUTAGE, and the shape is the fix. On 2026-08-03 the
        original form -- one `DELETE WHERE key IN (<aggregate over the whole
        table>)` -- took the warehouse out: it grouped 6.2M rows into ~5M groups,
        the mutation re-prepared that predicate for each of nine parts
        concurrently, and ClickHouse was OOM-killed by the kernel 30 seconds
        after the schedule fired. Every job that needed the warehouse then failed
        for 80 minutes, because nothing restarted it.

        Three changes, each doing a different job:

          1. Filter to keys that carry a tombstone before aggregating. ~4% of
             keys here, which took the doomed-key query from >2.70 GiB (never
             completed) to 148 MiB in 0.58s.
          2. Materialise the doomed set into a table, so the mutation's predicate
             is a small indexed lookup instead of that aggregate re-run per part.
          3. Delete per partition, serially, so each statement rewrites one day
             instead of the entire table.

        None of the three is sufficient alone, and the container also needs a
        memory limit for any of this to fail safely -- see the clickhouse service
        in deployment/docker-compose.yaml. With a limit, an over-large purge is a
        failed run; without one it is a dead warehouse.

        "Current state is deleted" rather than "has ever been tombstoned", and
        the difference is not hypothetical. A record can be deleted and then
        re-created under the same key -- a repo re-import does exactly this, and
        three such keys were present in a 5.4M-row snapshot. Selecting keys by
        `WHERE is_deleted = 1` matches those too, and the mutation deletes EVERY
        row for a matched key, so the later create goes with the tombstone: a
        live record silently destroyed by a housekeeping job.

        argMax over the version column asks the same question the engine asks --
        which version of this key wins -- so the purge agrees with what FINAL
        returns instead of approximating it. Same predicate for the count and the
        delete, so the number reported is the number removed.
        """
        keys = [order_by] if isinstance(order_by, str) else list(order_by)
        key_sql = ", ".join(f"`{k}`" for k in keys)
        table = f"`{self.database}`.`{name}`"
        staging = f"_purge_keys_{name}"

        # Only keys that carry a tombstone AT ALL can possibly be doomed, and on
        # this stream that is about 4% of them. Filtering to those before the
        # aggregate is the whole memory fix: the old form grouped all 6.2M rows
        # into ~5M groups and needed >2.70 GiB; this groups the ~236k keys that
        # could matter and peaks at 148 MiB, in 0.58s. Same answer, measured on
        # the same table.
        tombstoned = (
            f"SELECT {key_sql} FROM {table} WHERE `{is_deleted_column}` = 1"
        )
        # argMax asks the same question the engine asks -- which version of this
        # key wins -- so the purge agrees with what FINAL returns rather than
        # approximating it. Evaluated ACROSS THE WHOLE TABLE, deliberately: see
        # the note on partitions below.
        doomed_keys = (
            f"SELECT {key_sql} FROM {table} "
            f"WHERE ({key_sql}) IN ({tombstoned}) "
            f"GROUP BY {key_sql} "
            f"HAVING argMax(`{is_deleted_column}`, `{version_column}`) = 1"
        )

        client = self._get_client(self.database)
        try:
            if not int(client.command(f"EXISTS TABLE {table}")):
                return 0

            # The doomed set is materialised once, into a small ordinary table,
            # instead of being re-run as a subquery inside the mutation. A
            # mutation re-prepares its predicate for every part it touches, so
            # the old form paid that 6.2M-row aggregate per part -- nine times,
            # concurrently, which is what actually exhausted memory rather than
            # any single query.
            client.command(f"DROP TABLE IF EXISTS `{self.database}`.`{staging}`")
            client.command(
                f"CREATE TABLE `{self.database}`.`{staging}` "
                f"ENGINE = MergeTree ORDER BY ({key_sql}) "
                f"AS {doomed_keys}"
            )
            rows = client.query(f"SELECT count() FROM `{self.database}`.`{staging}`").result_rows
            doomed = int(rows[0][0]) if rows else 0
            if doomed == 0:
                return 0

            predicate = (
                f"({key_sql}) IN (SELECT {key_sql} FROM `{self.database}`.`{staging}`)"
            )

            # One mutation per partition rather than one over the table.
            #
            # A mutation rewrites every part it touches, so an unpartitioned
            # table means rewriting all of it to remove a few hundred thousand
            # keys. Partitioned by day, each statement rewrites one day and the
            # rest of the table is untouched -- and a partition holding no
            # doomed key is skipped outright by the predicate.
            #
            # THE DECISION IS STILL GLOBAL, and that distinction is the whole
            # correctness argument. A record created on Monday and retracted on
            # Tuesday has its two versions in two partitions. Deciding
            # per-partition would see the create alone on Monday (argMax 0,
            # spared) and the tombstone alone on Tuesday (argMax 1, deleted) --
            # which removes the tombstone and leaves the record, un-hiding a
            # post its author retracted. Exactly backwards. So the doomed set is
            # computed over the whole table first, and only the DELETE is
            # scoped.
            partitions = [
                row[0] for row in client.query(
                    "SELECT DISTINCT partition_id FROM system.parts "
                    "WHERE database = {db:String} AND table = {tbl:String} AND active "
                    "ORDER BY partition_id",
                    parameters={"db": self.database, "tbl": name},
                ).result_rows
            ]

            for partition_id in partitions:
                # mutations_sync = 2 so the asset does not report success while
                # the mutation is still queued -- the point is that it finished.
                # Serially, one partition at a time: nine concurrent rewrites is
                # how the previous version exhausted memory even when each one
                # would have fitted.
                client.command(
                    f"ALTER TABLE {table} "
                    f"DELETE IN PARTITION ID '{partition_id}' WHERE {predicate}",
                    settings={"mutations_sync": 2},
                )

            return doomed
        finally:
            # Dropped even on failure: a stale key table would be read by the
            # next run's mutation and delete keys that have since been re-created.
            try:
                client.command(f"DROP TABLE IF EXISTS `{self.database}`.`{staging}`")
            finally:
                client.close()

    def read_table(self, name: str) -> pd.DataFrame:
        """Read a whole warehouse table into a DataFrame."""
        return self.query_df(f"SELECT * FROM `{self.database}`.`{name}`")

    def query_df(self, sql: str) -> pd.DataFrame:
        """Run an arbitrary query against the warehouse and return a DataFrame."""
        # Reads take the session database so callers can leave table names unqualified.
        client = self._get_client(self.database)
        try:
            return client.query_df(sql)
        finally:
            client.close()
