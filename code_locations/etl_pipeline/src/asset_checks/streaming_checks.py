"""
Checks on the streaming path.

The rest of the platform's checks run against the synthetic ETL assets, which
made validation-gated execution something the repo demonstrated rather than
something it did to real data. These two run against the Bluesky firehose.

They read the counts the landing asset already computed rather than reopening
the Parquet -- see _landing_stats. A check that re-read the file would undo the
direct loader's whole reason for existing.

The two are deliberately different in kind, and only one of them is a gate:

    warehouse_keys_loadable  blocking. The columns ClickHouse cannot accept a
                             null in. A violation is the load failing.
    delete_ratio_in_bounds   advisory. A delete spike is surprising, not wrong,
                             and stopping ingest over it would be the error.

Treating every check as a gate is how blocking stops meaning anything.
"""
from typing import Optional

from dagster import AssetCheckResult, AssetCheckSeverity, MetadataValue, asset_check

from config.config import get_config
from ..warehouse_targets import records_target


def _nothing_landed(reason: str) -> AssetCheckResult:
    """
    An empty run is not a failed one.

    The extract returns None when Postgres had nothing above the watermark,
    which happens whenever the sensor fires slightly ahead of Spark. There is no
    data to be wrong about, so this passes rather than manufacturing a failure
    that would then gate the load.
    """
    return AssetCheckResult(passed=True, metadata={"skipped": MetadataValue.text(reason)})


@asset_check(asset="bsky_records_landed", name="warehouse_keys_loadable", blocking=True)
def check_warehouse_keys_loadable(bsky_records_landed: Optional[dict]) -> AssetCheckResult:
    """Every column the warehouse makes non-nullable is present and non-null.

    Blocking, and the gate is real: the sorting key, the version and the delete
    flag are stripped of Nullable when the table is built, so a null here is not
    a quality preference being violated, it is the INSERT failing. Catching it
    at the boundary turns a driver-level type error thrown from inside the
    ClickHouse client into a named check with counts attached.

    The column list is derived from the warehouse target rather than configured,
    so changing the sorting key moves the check with it.
    """
    if bsky_records_landed is None:
        return _nothing_landed("nothing landed; no rows to validate")

    target = records_target()
    stats = bsky_records_landed["stats"]
    missing = stats["missing_columns"]
    nulls = {c: n for c, n in stats["nulls"].items() if n}

    return AssetCheckResult(
        passed=not missing and not nulls,
        metadata={
            "rows": stats["rows"],
            "required_columns": MetadataValue.json(target.required_non_null()),
            "missing_columns": MetadataValue.json(missing),
            "columns_with_nulls": MetadataValue.json(nulls),
            "warehouse_table": target.table,
        },
    )


@asset_check(asset="bsky_records_landed", name="delete_ratio_in_bounds", blocking=False)
def check_delete_ratio_in_bounds(bsky_records_landed: Optional[dict]) -> AssetCheckResult:
    """The share of retractions in this batch is within its expected band.

    Deliberately NOT blocking. Deletes are a normal, constant part of the
    firehose -- around 3-4% of events -- and a spike means something worth
    looking at, not something worth halting ingest over. A mass retraction is
    real data that a reader should still see; refusing to load it because it was
    surprising would be the pipeline deciding what the truth is allowed to be.

    So this fails WARN rather than ERROR: it shows up in the UI and can be
    alerted on, and the load proceeds.

    The floor matters as much as the ceiling would: a batch that is *entirely*
    deletes is far more likely to be the producer mis-tagging every operation
    than the network retracting everything at once.
    """
    if bsky_records_landed is None:
        return _nothing_landed("nothing landed; no ratio to compute")

    stats = bsky_records_landed["stats"]
    rows, deletes = stats["rows"], stats["deletes"]
    if not rows:
        return _nothing_landed("empty batch; no ratio to compute")

    limit = float(get_config().get_max_delete_ratio())
    ratio = deletes / rows

    return AssetCheckResult(
        passed=ratio <= limit,
        severity=AssetCheckSeverity.WARN,
        metadata={
            "rows": rows,
            "deletes": deletes,
            "delete_ratio": round(ratio, 4),
            "max_delete_ratio": limit,
            "note": MetadataValue.text(
                "Advisory. ~3-4% is steady state for this firehose; a breach is "
                "an anomaly signal, not a correctness failure, so the load runs."
            ),
        },
    )
