"""
Unit tests for warehouse_loader: engine and column-type derivation, the
zone-driven choice of loader, and the SQL the direct path generates.
"""
from contextlib import contextmanager
from datetime import datetime

import pandas as pd
import pytest

from conftest import load_module

warehouse_loader = load_module("code_locations/shared/resources/warehouse_loader.py")
landing_zone = load_module("code_locations/shared/resources/landing_zone.py")

LandedFile = landing_zone.LandedFile
GlobSource = landing_zone.GlobSource
LoadResult = warehouse_loader.LoadResult
DirectLoader = warehouse_loader.DirectLoader
FrameLoader = warehouse_loader.FrameLoader

# Sub-second precision on purpose: the direct path formats %f and slices, and
# the slice is what these tests pin down.
LANDED_AT = datetime(2026, 8, 1, 16, 42, 58, 123456)

# What Parquet inference hands back for a typical CDC table: a bigint key, a
# nullable text column, a Postgres text[], a version and a delete flag.
DESCRIBED = [
    ("id", "Int64"),
    ("body", "Nullable(String)"),
    ("tags", "Array(Nullable(String))"),
    ("updated_at", "Nullable(DateTime64(6))"),
    ("is_deleted", "Nullable(UInt8)"),
]


def landed_file(dataset="etl_table", landed_at=LANDED_AT, start_id=101, end_id=200):
    name = f"{dataset}__{start_id:010d}-{end_id:010d}__20260801T164258Z.parquet"
    return LandedFile(
        uri=f"/app/landing/{dataset}/{name}",
        name=name,
        dataset=dataset,
        start_id=start_id,
        end_id=end_id,
        landed_at=landed_at,
    )


# --- fakes ------------------------------------------------------------------

class _Rows:
    def __init__(self, rows):
        self.result_rows = rows


class FakeClient:
    """
    Records every statement and answers the three queries _ingest makes.

    Anything else raises rather than returning an empty result, so a query the
    loader grows later shows up as a failure instead of a silent zero.
    """

    def __init__(self, described=(), existing=None, count=0):
        self.described = list(described)
        # A table the same load just created holds every described column, so
        # "no drift" is the default; pass `existing` to model a table that lags.
        self.existing = (
            [name for name, _ in self.described] if existing is None else list(existing)
        )
        self.count = count
        self.commands = []
        self.queries = []
        self.closed = False

    def query(self, sql, parameters=None):
        self.queries.append((sql, parameters))
        if sql.startswith("DESCRIBE TABLE"):
            return _Rows(list(self.described))
        if "system.columns" in sql:
            return _Rows([(name,) for name in self.existing])
        if sql.startswith("SELECT count()"):
            return _Rows([(self.count,)])
        raise AssertionError(f"unexpected query: {sql}")

    def command(self, sql):
        self.commands.append(sql)

    def close(self):
        self.closed = True

    def only_command(self, fragment):
        found = [sql for sql in self.commands if fragment in sql]
        assert len(found) == 1, f"expected one statement with {fragment!r}, got {found}"
        return found[0]


class FakeClickHouse:
    """Stands in for ClickHouseResource: a database name and a session."""

    def __init__(self, client=None, database="analytics"):
        self.database = database
        self.client = client or FakeClient()
        self.appended = []
        self.sessions = []

    @contextmanager
    def session(self, use_database=True):
        self.sessions.append(use_database)
        yield self.client

    def append_table(self, table, df, **kwargs):
        self.appended.append((table, df, kwargs))


class DirectZone:
    """Warehouse-readable: hands back a table-function expression."""

    def __init__(self, source="file('landing/etl_table/f.parquet', 'Parquet')", glob=None):
        self.source = source
        self.glob = glob
        self.asked_with = []

    def warehouse_source(self, landed):
        self.asked_with.append(landed)
        return self.source

    def warehouse_glob(self, dataset):
        return self.glob


class FrameZone:
    """Not warehouse-readable; serves frames the loader has to read itself."""

    def __init__(self, frames=None, files=()):
        self.frames = frames or {}
        self.files = list(files)
        self.asked_after = []

    def warehouse_source(self, landed):
        return None

    def warehouse_glob(self, dataset):
        return None

    def read(self, landed):
        return self.frames[landed.name]

    def files_after(self, dataset, watermark):
        self.asked_after.append((dataset, watermark))
        return list(self.files)


class BrokenZone:
    """A zone that cannot answer at all -- unconfigured region, absent bucket."""

    def warehouse_source(self, landed):
        raise RuntimeError("no region configured")


def direct_load(zone=None, client=None, landed=None, table="events", **kwargs):
    """Run DirectLoader.load against fakes and hand back everything to assert on."""
    zone = zone or DirectZone()
    client = client or FakeClient(described=DESCRIBED, count=7)
    clickhouse = FakeClickHouse(client)
    kwargs.setdefault("order_by", "id")
    result = DirectLoader(zone, clickhouse).load(table, landed or landed_file(), **kwargs)
    return result, client, zone


# --- _engine ----------------------------------------------------------------

def test_engine_without_version_is_bare_replacingmergetree():
    assert warehouse_loader.WarehouseLoader._engine(None, None) == "ReplacingMergeTree"


def test_engine_with_version_names_the_version_column():
    assert warehouse_loader.WarehouseLoader._engine("updated_at", None) == (
        "ReplacingMergeTree(`updated_at`)"
    )


def test_engine_with_version_and_delete_flag_names_both_in_order():
    assert warehouse_loader.WarehouseLoader._engine("updated_at", "is_deleted") == (
        "ReplacingMergeTree(`updated_at`, `is_deleted`)"
    )


def test_engine_rejects_delete_flag_without_a_version():
    # ClickHouse has no way to pick the current row for a key without a version,
    # so the combination is refused up front rather than producing a table that
    # collapses to an arbitrary row.
    with pytest.raises(ValueError, match="is_deleted_column requires version_column"):
        warehouse_loader.WarehouseLoader._engine(None, "is_deleted")


# --- _keys ------------------------------------------------------------------

def test_keys_wraps_a_single_column_name():
    assert warehouse_loader.WarehouseLoader._keys("id") == ["id"]


def test_keys_materialises_a_sequence_as_a_list():
    assert warehouse_loader.WarehouseLoader._keys(("tenant", "id")) == ["tenant", "id"]


# --- _strip_nullable --------------------------------------------------------

def test_strip_nullable_unwraps_a_top_level_nullable():
    assert warehouse_loader._strip_nullable("Nullable(String)") == "String"


@pytest.mark.parametrize(
    "ch_type",
    [
        "String",
        "DateTime64(3)",
        # Contains the word but is not nullable at the top level; unwrapping here
        # would corrupt the type rather than relax it.
        "Array(Nullable(String))",
        "Map(String, Nullable(Int64))",
    ],
)
def test_strip_nullable_leaves_everything_else_alone(ch_type):
    assert warehouse_loader._strip_nullable(ch_type) == ch_type


# --- _target_type -----------------------------------------------------------

def test_target_type_forces_the_delete_flag_to_uint8():
    # Whatever Parquet inferred, ReplacingMergeTree only reads UInt8 here.
    assert warehouse_loader._target_type(
        "is_deleted", "Nullable(Int64)", {"id"}, "is_deleted"
    ) == "UInt8"


def test_target_type_strips_nullable_from_a_sorting_key():
    assert warehouse_loader._target_type(
        "id", "Nullable(Int64)", {"id"}, None
    ) == "Int64"


def test_target_type_strips_nullable_from_the_version_column():
    assert warehouse_loader._target_type(
        "updated_at", "Nullable(DateTime64(6))", {"id", "updated_at"}, None
    ) == "DateTime64(6)"


def test_target_type_collapses_a_nullable_array_element():
    assert warehouse_loader._target_type(
        "tags", "Array(Nullable(String))", {"id"}, None
    ) == "Array(String)"


def test_target_type_leaves_an_ordinary_nullable_column_untouched():
    assert warehouse_loader._target_type(
        "body", "Nullable(String)", {"id"}, "is_deleted"
    ) == "Nullable(String)"


# --- build_loader / _zone_is_readable ---------------------------------------

def test_build_loader_picks_direct_for_a_warehouse_readable_zone():
    loader = warehouse_loader.build_loader(DirectZone(), FakeClickHouse(), {})
    assert isinstance(loader, DirectLoader)


def test_build_loader_falls_back_to_frames_when_the_zone_returns_none():
    loader = warehouse_loader.build_loader(FrameZone(), FakeClickHouse(), {})
    assert isinstance(loader, FrameLoader)


def test_build_loader_honours_direct_load_false_on_a_readable_zone():
    # The escape hatch: the warehouse can see the storage but cannot yet
    # authenticate to it.
    loader = warehouse_loader.build_loader(
        DirectZone(), FakeClickHouse(), {"direct_load": False}
    )
    assert isinstance(loader, FrameLoader)


def test_build_loader_defaults_to_direct_when_the_key_is_absent():
    loader = warehouse_loader.build_loader(
        DirectZone(), FakeClickHouse(), {"unrelated": "value"}
    )
    assert isinstance(loader, DirectLoader)


def test_build_loader_treats_a_raising_zone_as_unreadable():
    # A zone that blows up while answering is not a startup failure; it just
    # cannot serve the direct path.
    loader = warehouse_loader.build_loader(BrokenZone(), FakeClickHouse(), {})
    assert isinstance(loader, FrameLoader)


def test_zone_is_readable_probes_with_a_throwaway_landed_file():
    zone = DirectZone()
    assert warehouse_loader._zone_is_readable(zone) is True

    probe, = zone.asked_with
    assert isinstance(probe, warehouse_loader.LandedProbe)
    # The probe has to satisfy the attributes a real zone reads off a LandedFile.
    assert (probe.name, probe.dataset, probe.uri) == ("probe", "probe", "probe")


def test_zone_is_readable_needs_no_file_on_disk():
    # The real local zone, pointed at a directory that does not exist: the answer
    # comes from configuration alone, so this is safe to ask at startup.
    zone = landing_zone.LocalLandingZone("/nonexistent", warehouse_prefix="landing")
    assert warehouse_loader._zone_is_readable(zone) is True
    assert warehouse_loader._zone_is_readable(
        landing_zone.LocalLandingZone("/nonexistent")
    ) is False


# --- FrameLoader ------------------------------------------------------------

def test_frame_loader_stamps_provenance_on_every_row():
    landed = landed_file()
    frame = pd.DataFrame({"id": [1, 2, 3], "body": ["a", "b", "c"]})
    zone = FrameZone({landed.name: frame})
    clickhouse = FakeClickHouse()

    FrameLoader(zone, clickhouse).load("events", landed, order_by="id")

    _, inserted, _ = clickhouse.appended[0]
    assert inserted["_source_file"].tolist() == [landed.uri] * 3
    assert inserted["_landed_at"].tolist() == [landed.landed_at] * 3


def test_frame_loader_reports_the_row_count_of_the_file():
    landed = landed_file()
    zone = FrameZone({landed.name: pd.DataFrame({"id": [1, 2, 3]})})

    result = FrameLoader(zone, FakeClickHouse()).load("events", landed, order_by="id")

    assert result == LoadResult(3, [], landed.uri)


def test_frame_loader_forwards_the_engine_options_to_append_table():
    landed = landed_file()
    zone = FrameZone({landed.name: pd.DataFrame({"id": [1]})})
    clickhouse = FakeClickHouse()

    FrameLoader(zone, clickhouse).load(
        "events", landed, order_by=["tenant", "id"],
        version_column="updated_at", is_deleted_column="is_deleted",
        partition_by="toDate(ts)",
    )

    table, _, kwargs = clickhouse.appended[0]
    assert table == "events"
    # partition_by is forwarded too: the frame path and the direct path have to
    # create the SAME table, or a frame-loaded warehouse ends up unpartitioned
    # and purge_deleted's per-partition delete degenerates into a full rewrite.
    assert kwargs == {
        "order_by": ["tenant", "id"],
        "version_column": "updated_at",
        "is_deleted_column": "is_deleted",
        "partition_by": "toDate(ts)",
    }


def test_direct_loader_emits_partition_by_only_when_the_target_asks_for_one():
    # PARTITION BY has to sit between the engine and ORDER BY, or ClickHouse
    # rejects the DDL outright -- assert the ordering, not just the presence.
    _, client, _ = direct_load(partition_by="toDate(ts)")
    assert "PARTITION BY toDate(ts) ORDER BY (`id`)" in client.only_command("CREATE TABLE")

    _, client, _ = direct_load()
    assert "PARTITION BY" not in client.only_command("CREATE TABLE")


def test_frame_loader_skips_the_warehouse_entirely_for_an_empty_file():
    landed = landed_file()
    zone = FrameZone({landed.name: pd.DataFrame({"id": []})})
    clickhouse = FakeClickHouse()

    result = FrameLoader(zone, clickhouse).load("events", landed, order_by="id")

    assert result == LoadResult(0, [], landed.uri)
    assert clickhouse.appended == []


def test_frame_loader_does_not_mutate_the_frame_it_was_handed():
    landed = landed_file()
    frame = pd.DataFrame({"id": [1, 2]})
    zone = FrameZone({landed.name: frame})

    FrameLoader(zone, FakeClickHouse()).load("events", landed, order_by="id")

    assert list(frame.columns) == ["id"]


def test_frame_loader_replay_sums_rows_across_every_file():
    first, second = landed_file(start_id=1, end_id=2), landed_file(start_id=3, end_id=5)
    zone = FrameZone(
        frames={
            first.name: pd.DataFrame({"id": [1, 2]}),
            second.name: pd.DataFrame({"id": [3, 4, 5]}),
        },
        files=[first, second],
    )
    clickhouse = FakeClickHouse()

    result = FrameLoader(zone, clickhouse).replay(
        "events", "etl_table", order_by="id", from_id=17
    )

    assert result.rows_loaded == 5
    assert zone.asked_after == [("etl_table", 17)]
    assert len(clickhouse.appended) == 2


def test_frame_loader_replay_reports_the_file_count_as_its_source():
    landed = landed_file()
    zone = FrameZone({landed.name: pd.DataFrame({"id": [1]})}, files=[landed])

    result = FrameLoader(zone, FakeClickHouse()).replay("events", "etl_table", order_by="id")

    assert "1 file(s)" in result.source
    assert "FrameLoader" in result.source


# --- DirectLoader.load ------------------------------------------------------

def test_direct_load_refuses_a_zone_that_cannot_expose_the_file():
    # Reaching here means build_loader picked wrong; failing loudly beats
    # silently loading nothing.
    loader = DirectLoader(FrameZone(), FakeClickHouse())
    with pytest.raises(ValueError, match="cannot expose"):
        loader.load("events", landed_file(), order_by="id")


def test_direct_load_opens_the_session_without_a_default_database():
    # The database may not exist yet; connecting to it would fail before the
    # CREATE DATABASE that fixes it.
    clickhouse = FakeClickHouse(FakeClient(described=DESCRIBED, count=1))
    DirectLoader(DirectZone(), clickhouse).load("events", landed_file(), order_by="id")
    assert clickhouse.sessions == [False]


def test_direct_load_creates_the_table_with_derived_types_engine_and_order_by():
    _, client, _ = direct_load(
        order_by="id", version_column="updated_at", is_deleted_column="is_deleted"
    )

    create = client.only_command("CREATE TABLE")
    assert "`analytics`.`events`" in create
    assert "`id` Int64" in create
    assert "`body` Nullable(String)" in create
    assert "`tags` Array(String)" in create
    assert "`updated_at` DateTime64(6)" in create
    assert "`is_deleted` UInt8" in create
    assert "ENGINE = ReplacingMergeTree(`updated_at`, `is_deleted`)" in create
    assert create.endswith("ORDER BY (`id`)")


def test_direct_load_creates_the_database_before_the_table():
    _, client, _ = direct_load()
    assert client.commands[0] == "CREATE DATABASE IF NOT EXISTS `analytics`"
    assert "CREATE TABLE" in client.commands[1]


def test_direct_load_declares_provenance_columns_in_the_table():
    _, client, _ = direct_load()
    create = client.only_command("CREATE TABLE")
    assert "`_source_file` Nullable(String)" in create
    assert "`_landed_at` Nullable(DateTime64(3))" in create


def test_direct_load_inserts_provenance_last_in_both_lists():
    landed = landed_file()
    _, client, zone = direct_load(landed=landed)

    insert = client.only_command("INSERT INTO")
    columns = insert.split("(", 1)[1].split(")", 1)[0]
    assert columns == (
        "`id`, `body`, `tags`, `updated_at`, `is_deleted`, `_source_file`, `_landed_at`"
    )

    projection, _, from_clause = insert.partition(" FROM ")
    assert projection.endswith(
        f", '{landed.uri}', toDateTime64('2026-08-01 16:42:58.123', 3)"
    )
    assert from_clause == zone.source


def test_direct_load_truncates_the_landed_at_literal_to_milliseconds():
    # DateTime64(3) is the declared precision; handing it six digits would push
    # the microseconds into the value ClickHouse parses.
    _, client, _ = direct_load()
    insert = client.only_command("INSERT INTO")
    assert "toDateTime64('2026-08-01 16:42:58.123', 3)" in insert
    assert "123456" not in insert


def test_direct_load_selects_from_the_zones_source_expression():
    zone = DirectZone(source="s3('https://bucket.s3.eu-west-2.amazonaws.com/k', 'Parquet')")
    _, client, _ = direct_load(zone=zone)
    assert client.only_command("INSERT INTO").endswith(f" FROM {zone.source}")


def test_direct_load_reports_the_count_the_warehouse_read():
    result, client, zone = direct_load(client=FakeClient(described=DESCRIBED, count=42))
    assert result == LoadResult(42, [], zone.source)
    assert client.queries[-1][0] == f"SELECT count() FROM {zone.source}"


def test_direct_load_without_a_version_uses_the_bare_engine():
    _, client, _ = direct_load(order_by=["tenant", "id"])
    create = client.only_command("CREATE TABLE")
    assert "ENGINE = ReplacingMergeTree ORDER BY (`tenant`, `id`)" in create


# --- DirectLoader.replay ----------------------------------------------------

def glob_source():
    return GlobSource(
        source="file('landing/etl_table/*.parquet', 'Parquet')",
        uri_expr="concat('/app/landing/etl_table/', _file)",
        landed_at_expr="parseDateTimeBestEffortOrNull(extract(_file, 'stamp'))",
    )


_DEFAULT_GLOB = object()


def direct_replay(from_id=0, glob=_DEFAULT_GLOB, count=9):
    zone = DirectZone(glob=glob_source() if glob is _DEFAULT_GLOB else glob)
    client = FakeClient(described=DESCRIBED, count=count)
    result = DirectLoader(zone, FakeClickHouse(client)).replay(
        "events", "etl_table", order_by="id", from_id=from_id
    )
    return result, client, zone


def test_direct_replay_refuses_a_zone_with_no_glob():
    with pytest.raises(ValueError, match="cannot expose dataset etl_table"):
        direct_replay(glob=None)


def test_direct_replay_uses_the_globs_provenance_expressions_not_literals():
    glob = glob_source()
    _, client, _ = direct_replay(glob=glob)

    insert = client.only_command("INSERT INTO")
    assert f", {glob.uri_expr}, {glob.landed_at_expr} FROM {glob.source}" in insert
    # Nothing per-file can be known here, so no literal may leak in.
    assert "toDateTime64('" not in insert


def test_direct_replay_filters_rows_by_id_when_given_a_watermark():
    _, client, zone = direct_replay(from_id=100)
    insert = client.only_command("INSERT INTO")
    assert insert.endswith(f"FROM {zone.glob.source} WHERE `id` > 100")


def test_direct_replay_applies_the_same_predicate_to_the_count():
    _, client, zone = direct_replay(from_id=100)
    assert client.queries[-1][0] == f"SELECT count() FROM {zone.glob.source} WHERE `id` > 100"


def test_direct_replay_omits_the_where_clause_for_a_full_replay():
    _, client, _ = direct_replay(from_id=0)
    assert "WHERE" not in client.only_command("INSERT INTO")
    assert "WHERE" not in client.queries[-1][0]


def test_direct_replay_reports_the_glob_as_its_source():
    result, _, zone = direct_replay(count=1234)
    assert result == LoadResult(1234, [], zone.glob.source)


# --- _widen -----------------------------------------------------------------

def test_widen_alters_only_the_columns_the_table_lacks():
    client = FakeClient(existing=["id", "body"])

    added = warehouse_loader._widen(
        client, "analytics", "events",
        {"id": "Int64", "body": "Nullable(String)", "score": "Nullable(Float64)"},
    )

    assert added == ["score"]
    assert client.commands == [
        "ALTER TABLE `analytics`.`events` ADD COLUMN `score` Nullable(Float64)"
    ]


def test_widen_issues_one_alter_per_added_column():
    client = FakeClient(existing=["id"])

    added = warehouse_loader._widen(
        client, "analytics", "events",
        {"id": "Int64", "score": "Float64", "tags": "Array(String)"},
    )

    assert added == ["score", "tags"]
    assert len(client.commands) == 2


def test_widen_does_nothing_when_the_schema_is_unchanged():
    client = FakeClient(existing=["id", "body"])

    added = warehouse_loader._widen(
        client, "analytics", "events", {"id": "Int64", "body": "Nullable(String)"}
    )

    assert added == []
    assert client.commands == []


def test_widen_looks_the_table_up_by_bound_parameters():
    # The lookup is parameterised rather than interpolated, so a table name can
    # never reach system.columns as SQL.
    client = FakeClient(existing=[])
    warehouse_loader._widen(client, "analytics", "events", {})

    sql, parameters = client.queries[0]
    assert parameters == {"db": "analytics", "tbl": "events"}
    assert "events" not in sql


def test_direct_load_reports_widened_columns_in_the_result():
    # The table pre-exists without `tags`, so the load absorbs the drift and
    # says so in the result rather than widening silently.
    client = FakeClient(
        described=DESCRIBED,
        existing=["id", "body", "updated_at", "is_deleted"],
        count=3,
    )
    result, client, _ = direct_load(client=client)

    assert result.columns_added == ["tags"]
    assert "ADD COLUMN `tags` Array(String)" in client.only_command("ALTER TABLE")


# --- empty source -----------------------------------------------------------

def test_direct_load_returns_nothing_when_describe_comes_back_empty():
    client = FakeClient(described=[], count=99)
    result, client, zone = direct_load(client=client)

    assert result == LoadResult(0, [], zone.source)
    # No columns means no table worth creating and nothing to insert.
    assert client.commands == []


# --- identifier quoting -----------------------------------------------------

def test_generated_sql_backtick_quotes_reserved_column_and_table_names():
    client = FakeClient(described=[("order", "Int64"), ("group", "Nullable(String)")], count=1)
    _, client, _ = direct_load(client=client, table="order", order_by="order")

    create = client.only_command("CREATE TABLE")
    assert "`analytics`.`order`" in create
    assert "`order` Int64" in create
    assert "`group` Nullable(String)" in create
    assert create.endswith("ORDER BY (`order`)")

    insert = client.only_command("INSERT INTO")
    assert "INSERT INTO `analytics`.`order` (`order`, `group`," in insert
    assert "SELECT `order`, `group`," in insert


def test_widen_backtick_quotes_the_column_it_adds():
    client = FakeClient(existing=[])
    warehouse_loader._widen(client, "analytics", "order", {"group": "Int64"})
    assert client.commands == [
        "ALTER TABLE `analytics`.`order` ADD COLUMN `group` Int64"
    ]
