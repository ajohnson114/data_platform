from dagster import define_asset_job

# The `ingest` tag is what the instance's tag_concurrency_limits key off (see
# deployment/dagster.yaml). It is set here rather than relying on Dagster's
# built-in run tags so the limit is expressed against something this repo owns.
#
# The limit matters: both assets read a watermark, do work, then advance it. Two
# concurrent runs would read the same watermark and land the same id range
# twice. Nothing corrupts -- the landing file names carry their id range and the
# warehouse is a ReplacingMergeTree keyed on (did, rkey) -- but it is duplicated
# work against the operational database, which is exactly what the incremental
# read exists to avoid.
streaming_ingest_job = define_asset_job(
    name="streaming_ingest_job",
    selection=["bsky_records_landed", "bsky_records_snapshot"],
    tags={"ingest": "bsky"},
)
