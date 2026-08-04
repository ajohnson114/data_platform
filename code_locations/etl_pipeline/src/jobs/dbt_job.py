from dagster import AssetSelection, define_asset_job

# Rebuild the analytics models. Separate from streaming_ingest_job on purpose:
# ingest runs every 60 seconds against a firehose, and re-running four marts at
# that cadence would spend most of the warehouse's time re-aggregating data that
# has barely moved. The marts are read by humans and by the NL-to-SQL service,
# neither of which needs sub-minute freshness.
#
# Selected by group rather than by listing models, so a new .sql file joins this
# job by existing rather than by someone remembering to add it here.
dbt_job = define_asset_job(
    name="dbt_job",
    selection=AssetSelection.groups("analytics_dbt"),
)
