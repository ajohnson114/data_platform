from dagster import asset, MetadataValue

from ..warehouse_targets import records_target


@asset(group_name="streaming_ingest", required_resource_keys={"clickhouse"}, kinds={"clickhouse"})
def purge_deleted_records(context) -> None:
    """Physically remove posts their authors have deleted.

    The warehouse already hides them: a delete arrives as a tombstone and FINAL
    stops returning that key, so nothing downstream — the ML pipeline, the
    NL-to-SQL service — can see it. But hiding is not deleting. The original
    text is still sitting in the parts on disk, and for content a real person
    chose to retract, "you can't query it" is the wrong guarantee.

    So this runs on a schedule and does the real removal, dropping the tombstone
    and the record it retracts together. It is separate from the load, and
    scheduled rather than continuous, because a ClickHouse mutation rewrites
    every part it touches.

    That used to say "cheap once a day, ruinous every 60 seconds", which was
    half right and the wrong half. On 2026-08-03 it proved ruinous once a day
    too: the mutation's predicate was an aggregate over the whole 6.2M-row
    table, re-prepared per part, and the kernel OOM-killed ClickHouse thirty
    seconds after the schedule fired. The cadence was never what made it safe —
    the shape of the statement was, and it was the wrong shape. What changed is
    in ClickHouseResource.purge_deleted; the cadence is unchanged apart from
    moving off the hour so it stops colliding with the dbt rebuild.

    Deliberately not wired into the ingest job: purging is a housekeeping
    concern with its own cadence, and coupling it to ingest would tie how often
    data lands to how often parts get rewritten.

    SCOPE: this purges `bsky_records_snapshot` and nothing else.

    That was the whole story when the snapshot was the only table holding post
    text. It no longer is — `fct_posts` carries `text` too — so it is worth
    saying why that table is not listed here, because the omission looks like an
    oversight and isn't.

    `fct_posts` is a dbt table rebuilt every five minutes from a view that
    applies FINAL, and FINAL does not return tombstoned keys. So each rebuild
    reconstructs the table without the retracted rows and drops the old one,
    which releases the parts holding their text. The marts are therefore purged
    on a five-minute cycle by ordinary operation, and adding them to this daily
    mutation would replace that with something twenty-four hours slower.

    Measured mid-cycle: 29 retracted posts present in `fct_posts` against 36,436
    currently tombstoned in the snapshot — i.e. only the ones retracted since the
    last rebuild, which is exactly the expected residue.

    The dependency to be aware of: that guarantee comes from `fct_posts` being
    full-refresh. Making it incremental would turn this residue into a
    permanently accumulating pile of retracted text and would make extending
    this asset to cover the marts a correctness requirement rather than a
    redundancy. See dbt/README.md, "Why fct_posts and dim_authors are not
    incremental".

    `dim_authors` and `agg_activity_by_minute` hold counts only, no record
    content, so nothing needs purging there.
    """
    clickhouse = context.resources.clickhouse
    # From the same target the loader built the table from, so the purge cannot
    # dedupe on a different key or version than the engine does.
    target = records_target()

    purged = clickhouse.purge_deleted(
        target.table,
        order_by=target.order_by,
        version_column=target.version_column,
        is_deleted_column=target.is_deleted_column,
    )

    context.add_output_metadata({
        "table": f"{clickhouse.database}.{target.table}",
        "keys_purged": purged,
        "note": MetadataValue.text(
            "0 is the healthy steady state — it means every retraction seen so far "
            "has already been removed from disk. Counts keys whose CURRENT version "
            "is a tombstone, so a record deleted and later re-created is spared."
        ),
    })
