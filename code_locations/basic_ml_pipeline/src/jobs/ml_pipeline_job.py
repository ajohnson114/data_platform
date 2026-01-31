from dagster import define_asset_job

ml_pipeline_job = define_asset_job(
    name="ml_pipeline_job", selection=["pull_data_from_postgres","fit_model","save_model"]
)