from dagster import define_asset_job

streaming_ingest_job = define_asset_job(
    name="streaming_ingest_job", selection=["crypto_prices_snapshot"]
)
