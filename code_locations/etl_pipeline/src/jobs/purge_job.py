from dagster import define_asset_job

# Both purges, one job, one schedule. They are the same concern -- making a
# retraction real everywhere the record was written -- split across two stores
# only because one is a ClickHouse mutation and the other is deleting files.
# Running them together means a reader of the run history sees one answer to
# "did housekeeping happen last night" rather than two to reconcile.
purge_job = define_asset_job(
    name="purge_job", selection=["purge_deleted_records", "purge_landing_archive"]
)
