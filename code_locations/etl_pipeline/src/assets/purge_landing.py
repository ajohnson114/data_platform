from datetime import datetime, timedelta, timezone

from dagster import MetadataValue, asset

from config.config import get_config
from ..warehouse_targets import all_targets


@asset(
    group_name="streaming_ingest",
    required_resource_keys={"landing_zone"},
    kinds={"parquet"},
)
def purge_landing_archive(context) -> None:
    """Drop landed Parquet files past the retention horizon.

    The sibling of purge_deleted_records, and it exists because that asset
    cannot reach here. purge_deleted_records removes retracted records from the
    warehouse; the landing zone holds the raw change log those records arrived
    in, so a deleted post's text sits in a Parquet file indefinitely regardless
    of what the warehouse does. Measured on a three-hour archive: 11,080
    retracted posts still had their text in the landing files, against 1,466 in
    the warehouse. The archive was the larger exposure by almost 8x.

    Retention rather than surgical deletion, and the distinction is the whole
    reason this is a separate asset. Editing a landed file to remove one record
    would break the property the archive is built on -- files are written once
    and never rewritten, which is what makes a replay reproduce exactly what the
    live load saw. Dropping a whole file keeps that intact: replay loses reach,
    not trust. It can go back as far as the horizon and no further, which is a
    limit worth stating rather than a guarantee quietly broken.

    It is also a disk decision. At firehose rate the zone grows by gigabytes a
    day, and nothing bounded it before.

    Runs on the same daily schedule as the warehouse purge, for the same reason
    the warehouse purge is daily: this is housekeeping, and coupling it to
    ingest would tie how often data lands to how often files get removed.
    """
    landing = context.resources.landing_zone
    days = int(get_config().get_landing_retention_days())
    cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=days)

    # Every dataset, not just the firehose. etl_table is synthetic and tiny, but
    # a retention policy that silently applied to one dataset and not another is
    # the kind of thing nobody discovers until they need the files.
    removed, kept = {}, {}
    for target in all_targets():
        dataset = target.dataset
        doomed = landing.purge_before(dataset, cutoff)
        removed[dataset] = len(doomed)
        kept[dataset] = len(landing.files(dataset))

    context.add_output_metadata({
        "retention_days": days,
        "cutoff": cutoff.isoformat(),
        "files_removed": MetadataValue.json(removed),
        "files_remaining": MetadataValue.json(kept),
        "note": MetadataValue.text(
            "The newest file of each dataset is never removed, whatever its age. "
            "The extract's watermark is max(end_id) read off this listing, so an "
            "empty directory would silently reset it to 0 and re-land the source "
            "from the beginning."
        ),
    })
