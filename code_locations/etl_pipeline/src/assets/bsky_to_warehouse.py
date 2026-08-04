from datetime import timedelta
from typing import Optional

import pandas as pd
from dagster import FreshnessPolicy, asset, MetadataValue
from sqlalchemy import text
from config.config import get_config
from ..warehouse_targets import LOAD_RETRY, records_target

_freshness = get_config().get_snapshot_freshness()

# The other half of the pair described in src/dbt.py.
#
# No automation attached: bsky_record_sensor already drives this asset every 60
# seconds, and a second thing requesting it would race the sensor for the same
# watermark. This is purely an alarm, and it earns its place by disambiguating
# the marts' alarm rather than by triggering anything:
#
#   snapshot PASS, marts FAIL  ->  dbt is broken
#   snapshot FAIL, marts PASS  ->  ingest is down; the marts are faithfully
#                                  reflecting a stream that stopped
#   both FAIL                  ->  ingest has been down long enough that the
#                                  marts cannot be current either
#
# Without a policy here, the middle case reads identically to the first: the
# marts go red and nothing on the graph says the cause is upstream of dbt.
SNAPSHOT_FRESHNESS = FreshnessPolicy.time_window(
    fail_window=timedelta(minutes=float(_freshness["fail_after_minutes"])),
    warn_window=timedelta(minutes=float(_freshness["warn_after_minutes"])),
)


def _landing_stats(df, target) -> dict:
    """
    Everything the asset checks need, computed where the frame already is.

    Deliberately here and not in the checks themselves. The whole point of the
    direct loader is that the landed Parquet never gets pulled through this
    process -- ClickHouse opens it. A check that re-read the file to count nulls
    would hand that back, paying the full read every run to produce four
    integers. These rows are already in memory because the extract just wrote
    them, so the counts are free and the checks become pure assertions over
    numbers rather than another pass over the data.
    """
    required = target.required_non_null()
    return {
        "rows": len(df),
        "missing_columns": [c for c in required if c not in df.columns],
        "nulls": {c: int(df[c].isna().sum()) for c in required if c in df.columns},
        "deletes": int((df["operation"] == "delete").sum()) if "operation" in df.columns else 0,
    }


@asset(group_name="streaming_ingest", required_resource_keys={"etl_postgres", "landing_zone"}, kinds={"postgres", "parquet"})
def bsky_records_landed(context) -> Optional[dict]:
    """Extract new Bluesky change events from Postgres into an immutable Parquet file.

    The sensor fires this every 60 seconds against a firehose, so each run lands
    a file covering only what Spark has written since the last one. Creates,
    updates and deletes are all landed: this is an append-only log of what
    happened, not a picture of what currently exists.

    Returns the file it wrote, which is the load's only input. That makes the
    dependency a real one rather than a `deps=` string -- the downstream asset
    receives the exact file to load and never has to work out for itself which
    files are outstanding.
    """
    etl_postgres = context.resources.etl_postgres
    landing = context.resources.landing_zone
    table_name = get_config().get_bsky_records_table_name()
    dataset = get_config().get_records_landing_dataset()

    # The extract's bookmark, read straight off the file listing -- no catalog
    # and no sidecar state store. This is the only watermark left in the
    # pipeline; the load doesn't need one.
    watermark = landing.max_landed_id(dataset)

    with etl_postgres.get_engine().begin() as conn:
        df = pd.read_sql(
            text(get_config().get_read_bsky_records()),
            conn,
            params={"watermark": int(watermark),
                    "batch_size": int(get_config().get_landing_batch_size())},
        )

    if df.empty:
        context.add_output_metadata({
            "table_source": table_name,
            "rows_landed": 0,
            "watermark": int(watermark),
            "file": MetadataValue.text("nothing new to land"),
        })
        return None

    landed = landing.write(dataset, df)
    stats = _landing_stats(df, records_target())

    operations = df["operation"].value_counts().to_dict() if "operation" in df.columns else {}
    context.add_output_metadata({
        "table_source": table_name,
        "rows_landed": len(df),
        "watermark_from": int(watermark),
        "watermark_to": landed.end_id,
        "file": MetadataValue.path(landed.uri),
        "landed_at": landed.landed_at.isoformat(),
        "operations": MetadataValue.json({k: int(v) for k, v in operations.items()}),
    })

    # An envelope, not the LandedFile dict directly: the checks read `stats` and
    # the load reads `file`, so neither has to touch the Parquet again.
    #
    # Plain dicts rather than the namedtuple itself because this crosses the io
    # manager as a pickle, and a dict is the one shape that cannot go stale
    # against a change to LandedFile between the two steps of a run.
    return {"file": landed._asdict(), "stats": stats}


@asset(
    group_name="streaming_ingest",
    required_resource_keys={"landing_zone", "warehouse_loader"},
    retry_policy=LOAD_RETRY,
    kinds={"parquet", "clickhouse"},
    freshness_policy=SNAPSHOT_FRESHNESS,
)
def bsky_records_snapshot(context, bsky_records_landed: Optional[dict]) -> None:
    """Fold the landed change events into current state in ClickHouse.

    Creates, updates and deletes all arrive as ordinary appended rows; the
    warehouse table is a ReplacingMergeTree keyed on (did, rkey) and versioned by
    the event timestamp, so the newest event for a record wins and a delete
    tombstone hides it. That is log/table duality: the landing zone stays the
    log, this table is the table.

    Loads exactly the file the extract just wrote. There is no watermark here on
    purpose: the file is complete or absent (landing.write renames into place),
    re-inserting is harmless because the sorting key dedupes, and a failure that
    outlives the retry policy shows up as a failed run rather than as a quiet
    catch-up nobody sees. Replaying the whole zone is a separate, deliberate job
    -- see rebuild_warehouse_from_landing.
    """
    if bsky_records_landed is None:
        context.add_output_metadata({"rows_loaded": 0, "file": MetadataValue.text("nothing landed")})
        return

    landed = context.resources.landing_zone.landed_file(bsky_records_landed["file"])
    loader = context.resources.warehouse_loader
    target = records_target()

    result = loader.load(
        target.table,
        landed,
        order_by=target.order_by,
        version_column=target.version_column,
        is_deleted_column=target.is_deleted_column,
        partition_by=target.partition_by,
    )

    context.add_output_metadata({
        "table": target.table,
        "rows_loaded": result.rows_loaded,
        "source_file": MetadataValue.path(landed.uri),
        # Which loader ran, and against what. The direct path reports a table
        # function; the frame path reports the file it pulled through pandas.
        "loader": type(loader).__name__,
        "warehouse_source": MetadataValue.text(str(result.source)),
        "columns_added": MetadataValue.json(result.columns_added),
    })
