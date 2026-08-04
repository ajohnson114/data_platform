"""
What each landed dataset becomes in the warehouse.

One definition, read by the two snapshot assets and by the rebuild job, so a
replay cannot dedupe on a different key than the live load did -- which would
silently produce a differently-shaped table rather than an error.
"""
from collections import namedtuple

from dagster import Backoff, RetryPolicy

from config.config import get_config

# The load is the only step that touches something outside the process, so it is
# the step that fails for reasons that go away on their own -- ClickHouse
# restarting, a dropped connection. Retrying in place is what makes the
# file-per-run design safe without a load watermark: the run recovers itself
# instead of leaving the file for a human to notice. Re-inserting is harmless
# because the warehouse dedupes on the sorting key.
#
# Here rather than in either asset module so both snapshot assets share one
# definition and neither imports the other to get it.
LOAD_RETRY = RetryPolicy(max_retries=3, delay=30, backoff=Backoff.EXPONENTIAL)

# order_by is the ReplacingMergeTree sorting key and therefore the dedup key.
# version_column makes the highest version win for a key instead of an arbitrary
# row, and is_deleted_column marks tombstones so FINAL hides them.
class WarehouseTarget(
    namedtuple(
        "WarehouseTarget",
        "dataset table order_by version_column is_deleted_column partition_by",
        # Most targets are small enough that partitioning buys nothing, so it is
        # opt-in rather than something every target has to think about.
        defaults=(None,),
    )
):
    def required_non_null(self) -> list:
        """
        Columns the warehouse cannot accept a null in.

        Not a policy choice -- it falls out of the engine. ClickHouse rejects a
        Nullable sorting key outright, ReplacingMergeTree needs a version to pick
        a winner, and the delete flag has to be exactly UInt8. So these are
        precisely the columns the loader strips Nullable from (see _target_type),
        and a null arriving in one of them is the load failing, not a preference
        being violated.

        Deriving the check from the same place the DDL derives from means the two
        cannot drift: change the sorting key and the check follows.
        """
        keys = [self.order_by] if isinstance(self.order_by, str) else list(self.order_by)
        return [c for c in (*keys, self.version_column, self.is_deleted_column) if c]


def records_target() -> WarehouseTarget:
    cfg = get_config()
    return WarehouseTarget(
        dataset=cfg.get_records_landing_dataset(),
        table=cfg.get_records_table_name(),
        # The AT Protocol identity of a record, not our ingestion id: it is what
        # makes an edit or a delete land on the row it refers to.
        order_by=cfg.get_records_key(),
        version_column=cfg.get_records_version_column(),
        is_deleted_column="is_deleted",
        # One partition per day of event time, derived from the version column
        # so it cannot name a column the engine does not already require.
        #
        # This is for the PURGE, not for reads. A ClickHouse mutation rewrites
        # every part it touches, and with one unpartitioned table that means the
        # whole table for a delete of a few hundred thousand keys. Partitioned,
        # `DELETE IN PARTITION` rewrites one day. On 2026-08-03 the unpartitioned
        # form took the warehouse out with an OOM; see purge_deleted.
        #
        # Day rather than month because it also gives retention somewhere to
        # stand later: dropping a partition is metadata, not a mutation.
        partition_by=f"toDate(fromUnixTimestamp64Micro(`{cfg.get_records_version_column()}`))",
    )


def etl_target() -> WarehouseTarget:
    cfg = get_config()
    return WarehouseTarget(
        dataset=cfg.get_etl_landing_dataset(),
        table=cfg.get_etl_snapshot_table_name(),
        # Append-only with no in-place changes, so the ingestion id is the whole
        # identity and there is no version to compare.
        order_by="id",
        version_column=None,
        is_deleted_column=None,
    )


def all_targets() -> list:
    return [records_target(), etl_target()]
