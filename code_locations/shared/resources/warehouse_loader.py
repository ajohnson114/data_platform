# shared/resources/warehouse_loader.py
"""
Loading one landed Parquet file into the warehouse.

Two implementations of one job, picked by what the landing zone can offer:

    DirectLoader   ClickHouse opens the file itself, via file() locally and s3()
                   on AWS. The bytes never enter this process.
    FrameLoader    This process reads the file into pandas and inserts it. The
                   fallback, for a zone the warehouse cannot reach.

The pairing is derived, never configured. A direct load only works if the
warehouse can actually see the storage the zone writes to, and only the zone
knows that -- so the zone answers `warehouse_source(landed)` and build_loader
picks from the answer. Configuring the two independently would make
`fs landing + direct load` expressible, and it would fail at materialization
time, in the cluster, which is exactly the failure class this repo keeps trying
to move to startup.

One file per call. There is no watermark and no batch here: the landing asset
hands the load the file it just wrote, and landing.write() renames into place so
a file is complete or absent, never half-read. A retry re-inserts the same rows
and the warehouse's ReplacingMergeTree collapses them, which is what makes the
whole thing safe to just run again.
"""
from __future__ import annotations

from collections import namedtuple

# rows_loaded is how many rows the FILE held, which is deliberately not the same
# as how many rows the table gained. ClickHouse has optimize_on_insert on by
# default, so a ReplacingMergeTree collapses same-key rows within the inserted
# block: a create and its later delete arriving in one landed file go in as two
# rows and come to rest as one. Both loaders report the source count, so the
# number means the same thing on either path -- but expect the table to hold
# fewer, and more so on a busy batch.
#
# columns_added records schema drift the load absorbed, so a widened table shows
# up in the asset's metadata rather than silently happening.
LoadResult = namedtuple("LoadResult", "rows_loaded columns_added source")

# Stamped on every warehouse row so a value traces back to the exact file it
# arrived in. Nullable on both paths so the two loaders agree on the schema.
_PROVENANCE = {
    "_source_file": "Nullable(String)",
    "_landed_at": "Nullable(DateTime64(3))",
}


class WarehouseLoader:
    """Shared plumbing; subclasses implement load()."""

    def __init__(self, zone, clickhouse):
        self.zone = zone
        self.clickhouse = clickhouse

    def load(self, table, landed, *, order_by, version_column=None,
             is_deleted_column=None, partition_by=None) -> LoadResult:
        raise NotImplementedError

    def replay(self, table, dataset, *, order_by, version_column=None,
               is_deleted_column=None, partition_by=None, from_id=0) -> LoadResult:
        """
        Reload a whole dataset from the archive.

        Separate from load() because it is a different shape of operation, not a
        different amount of one: load() is handed a file, replay() is handed a
        dataset and works out the rest. Keeping them apart is what stops the
        listing and globbing machinery leaking into a path that runs every 60
        seconds to consume exactly one known file.

        Safe to re-run: everything already present collapses on the sorting key
        rather than doubling.
        """
        raise NotImplementedError

    @staticmethod
    def _keys(order_by) -> list:
        return [order_by] if isinstance(order_by, str) else list(order_by)

    @classmethod
    def _engine(cls, version_column, is_deleted_column) -> str:
        if is_deleted_column and not version_column:
            raise ValueError(
                "is_deleted_column requires version_column: ClickHouse needs a "
                "version to know which row for a key is the current one"
            )
        if version_column and is_deleted_column:
            return f"ReplacingMergeTree(`{version_column}`, `{is_deleted_column}`)"
        if version_column:
            return f"ReplacingMergeTree(`{version_column}`)"
        return "ReplacingMergeTree"


class FrameLoader(WarehouseLoader):
    """
    Read the file here, insert it from pandas.

    The original path, kept as the fallback for a landing zone the warehouse
    cannot read -- and as the escape hatch (`direct_load: false`) if the direct
    path is unavailable in an environment. Every byte crosses this process, so
    it is bounded by the container's memory rather than by the warehouse's.
    """

    def load(self, table, landed, *, order_by, version_column=None,
             is_deleted_column=None, partition_by=None) -> LoadResult:
        df = self.zone.read(landed)
        if df.empty:
            return LoadResult(0, [], landed.uri)

        # Per row rather than per file, so provenance survives any later merge,
        # filter or join in the warehouse.
        df = df.copy()
        df["_source_file"] = landed.uri
        df["_landed_at"] = landed.landed_at

        self.clickhouse.append_table(
            table,
            df,
            order_by=order_by,
            version_column=version_column,
            is_deleted_column=is_deleted_column,
            partition_by=partition_by,
        )
        return LoadResult(len(df), [], landed.uri)

    def replay(self, table, dataset, *, order_by, version_column=None,
               is_deleted_column=None, partition_by=None, from_id=0) -> LoadResult:
        """One file at a time -- there is no parallel read to gain without the
        warehouse doing the reading."""
        rows = 0
        files = self.zone.files_after(dataset, from_id)
        for landed in files:
            result = self.load(
                table, landed, order_by=order_by,
                version_column=version_column, is_deleted_column=is_deleted_column,
                partition_by=partition_by,
            )
            rows += result.rows_loaded
        return LoadResult(rows, [], f"{len(files)} file(s) via {type(self).__name__}")


class DirectLoader(WarehouseLoader):
    """
    Push the read into the warehouse: INSERT ... SELECT over a table function.

    ClickHouse opens the Parquet itself and parallelises the read, so the load
    costs this process one round trip and no memory regardless of file size.
    Same code path locally and on AWS -- only the table function differs -- which
    is the point: a bug here is reproducible under `make` rather than only in
    the cluster.
    """

    def load(self, table, landed, *, order_by, version_column=None,
             is_deleted_column=None, partition_by=None) -> LoadResult:
        source = self.zone.warehouse_source(landed)
        if source is None:
            raise ValueError(
                f"{type(self.zone).__name__} cannot expose {landed.name} to the "
                f"warehouse; build_loader should not have selected DirectLoader"
            )

        # Provenance as literals: this path knows the file, so there is nothing
        # to recover from the name.
        landed_at = landed.landed_at.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
        return self._ingest(
            table, source,
            uri_expr=_sql_string(landed.uri),
            landed_at_expr=f"toDateTime64({_sql_string(landed_at)}, 3)",
            order_by=order_by, version_column=version_column,
            is_deleted_column=is_deleted_column, partition_by=partition_by,
        )

    def replay(self, table, dataset, *, order_by, version_column=None,
               is_deleted_column=None, partition_by=None, from_id=0) -> LoadResult:
        """
        The whole archive in one statement, read by the warehouse in parallel.

        This is where a glob is the right tool and the per-file path is not.
        Replay is rare, deliberate, and unbounded in size -- exactly the case
        that wants the warehouse fanning out across objects rather than this
        process issuing one round trip each. The steady-state load stays
        file-at-a-time because it already knows its one file.

        `from_id` filters rows rather than files: the glob still opens every
        object, but Parquet row-group statistics on `id` let ClickHouse skip
        most of them without reading. A partial replay is rarer still, so the
        simpler predicate wins over pruning the file list.
        """
        glob = self.zone.warehouse_glob(dataset)
        if glob is None:
            raise ValueError(
                f"{type(self.zone).__name__} cannot expose dataset {dataset} to "
                f"the warehouse; build_loader should not have selected DirectLoader"
            )

        return self._ingest(
            table, glob.source,
            uri_expr=glob.uri_expr,
            landed_at_expr=glob.landed_at_expr,
            order_by=order_by, version_column=version_column,
            is_deleted_column=is_deleted_column, partition_by=partition_by,
            where=f"`id` > {int(from_id)}" if from_id else None,
        )

    def _ingest(self, table, source, *, uri_expr, landed_at_expr, order_by,
                version_column=None, is_deleted_column=None, partition_by=None,
                where=None) -> LoadResult:
        """
        Shared by both paths. The only difference between loading one file and
        replaying a dataset is the table function and how provenance is derived
        -- everything else (schema discovery, DDL, drift, the insert) is
        identical, so it lives here rather than being written twice and drifting.
        """
        db = self.clickhouse.database
        keys = self._keys(order_by)
        non_nullable = set(keys)
        if version_column:
            non_nullable.add(version_column)
        if is_deleted_column:
            non_nullable.add(is_deleted_column)

        with self.clickhouse.session(use_database=False) as client:
            # DESCRIBE reads the Parquet footer, not the data, so schema
            # discovery is metadata-only. This is the direct path's equivalent
            # of reading dtypes off a DataFrame.
            described = [
                (row[0], row[1])
                for row in client.query(f"DESCRIBE TABLE {source}").result_rows
            ]
            if not described:
                return LoadResult(0, [], source)

            columns = {
                name: _target_type(name, ch_type, non_nullable, is_deleted_column)
                for name, ch_type in described
            }

            client.command(f"CREATE DATABASE IF NOT EXISTS `{db}`")
            ddl = ", ".join(
                f"`{c}` {t}" for c, t in {**columns, **_PROVENANCE}.items()
            )
            # PARTITION BY before ORDER BY, and only when the target asked
            # for one. It bounds what a later DELETE has to rewrite -- see
            # ClickHouseResource.purge_deleted -- and is inert for reads.
            partition_clause = f"PARTITION BY {partition_by} " if partition_by else ""
            client.command(
                f"CREATE TABLE IF NOT EXISTS `{db}`.`{table}` ({ddl}) "
                f"ENGINE = {self._engine(version_column, is_deleted_column)} "
                f"{partition_clause}"
                f"ORDER BY ({', '.join(f'`{k}`' for k in keys)})"
            )

            # Schema drift, same policy as append_table: widen for new columns,
            # leave departed ones in place going null. Types are inferred from
            # Parquet here rather than from pandas, so a column may land as
            # DateTime64(6) where the frame loader would have said (3). Both are
            # accepted on insert; only the first loader to see a column decides.
            added = _widen(client, db, table, columns)

            names = list(columns)
            select_list = ", ".join(f"`{c}`" for c in names)
            insert_list = ", ".join(f"`{c}`" for c in names + list(_PROVENANCE))
            predicate = f" WHERE {where}" if where else ""

            client.command(
                f"INSERT INTO `{db}`.`{table}` ({insert_list}) "
                f"SELECT {select_list}, {uri_expr}, {landed_at_expr} "
                f"FROM {source}{predicate}"
            )

            # Parquet carries its row count in the footer, so an unfiltered count
            # is metadata-only. With a predicate it has to read the `id` column,
            # which row-group statistics keep cheap.
            rows = client.query(f"SELECT count() FROM {source}{predicate}").result_rows
            loaded = int(rows[0][0]) if rows else 0

        return LoadResult(loaded, added, source)


def _sql_string(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace("'", "\\'")
    return f"'{escaped}'"


def _strip_nullable(ch_type: str) -> str:
    if ch_type.startswith("Nullable(") and ch_type.endswith(")"):
        return ch_type[len("Nullable("):-1]
    return ch_type


def _target_type(name, ch_type, non_nullable, is_deleted_column) -> str:
    """
    Turn an inferred Parquet column type into the warehouse column type.

    Three rules, the same ones append_table applies coming from pandas:
    the delete flag must be exactly UInt8 for ReplacingMergeTree to read it;
    sorting-key and version columns cannot be Nullable at all; and ClickHouse
    has no Nullable inside a sorting key or Nullable(Array(...)) anywhere, so
    array elements are collapsed to non-null -- Parquet inference hands back
    Array(Nullable(String)) for a Postgres text[].
    """
    if name == is_deleted_column:
        return "UInt8"
    if ch_type.startswith("Array(") and ch_type.endswith(")"):
        return f"Array({_strip_nullable(ch_type[len('Array('):-1])})"
    if name in non_nullable:
        return _strip_nullable(ch_type)
    return ch_type


def _widen(client, database, table, columns) -> list:
    """Add any column the file has and the table doesn't. Returns what it added."""
    existing = {
        row[0]
        for row in client.query(
            "SELECT name FROM system.columns "
            "WHERE database = {db:String} AND table = {tbl:String}",
            parameters={"db": database, "tbl": table},
        ).result_rows
    }
    added = []
    for name, ch_type in columns.items():
        if name not in existing:
            client.command(
                f"ALTER TABLE `{database}`.`{table}` ADD COLUMN `{name}` {ch_type}"
            )
            added.append(name)
    return added


def build_loader(zone, clickhouse, cfg: dict) -> WarehouseLoader:
    """
    Pick the loader from what the zone can offer, the way build_landing_zone and
    build_io_manager pick from config.

    Deliberately not a config `type` key. The valid combinations are not a cross
    product -- there is no such thing as a direct load out of a local zone the
    warehouse cannot see -- so the choice is derived from the zone and the
    impossible pairing is unrepresentable rather than validated.

    `direct_load: false` forces the frame path. That exists for an environment
    where the warehouse has the storage mounted but cannot authenticate to it
    yet, which is a real state to be in mid-rollout and not one worth failing on.
    """
    if cfg.get("direct_load", True) and _zone_is_readable(zone):
        return DirectLoader(zone, clickhouse)
    return FrameLoader(zone, clickhouse)


def _zone_is_readable(zone) -> bool:
    """
    Whether the zone can hand the warehouse a source expression.

    Asked with a throwaway probe rather than a separate `supports_direct()`
    predicate, so there is exactly one method to implement on a new zone and no
    way for the two to disagree.
    """
    probe = LandedProbe()
    try:
        return zone.warehouse_source(probe) is not None
    except Exception:
        return False


class LandedProbe:
    """A stand-in LandedFile, for asking a zone what it can do."""
    uri = name = dataset = "probe"
    start_id = end_id = 0
    landed_at = None
