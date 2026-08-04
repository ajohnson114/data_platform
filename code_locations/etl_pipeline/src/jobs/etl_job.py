from dagster import define_asset_job

etl_job = define_asset_job(
    name="etl_job", selection=["prepare_postgres_tables", "pull_data_from_source","clean_data","save_data_to_postgres_db","etl_table_landed","etl_table_snapshot"]
)