from dagster import sensor, RunRequest, DefaultSensorStatus, SensorEvaluationContext
from sqlalchemy import text
from shared.resources.db_client_resource import DBClientResource
from config.config import get_config

# Reuse the same Postgres connection pattern as the rest of the ETL pipeline
_db_client = DBClientResource(**get_config().get_postgres_creds())

@sensor(
    job_name="streaming_ingest_job",
    minimum_interval_seconds=60,
    default_status=DefaultSensorStatus.RUNNING,
)
def crypto_price_sensor(context: SensorEvaluationContext):
    """Sensor that detects new rows in the crypto_prices Postgres table and triggers DuckDB ingest."""
    table_name = get_config().get_crypto_table_name()

    try:
        with _db_client.get_engine().begin() as conn:
            result = conn.execute(text(f"SELECT COUNT(*) FROM {table_name}")).fetchone()
            current_count = result[0] if result else 0
    except Exception as e:
        context.log.warning(f"Could not query {table_name}: {e}")
        return

    last_count = int(context.cursor) if context.cursor else 0

    if current_count > last_count:
        context.log.info(f"New rows detected: {last_count} -> {current_count}")
        context.update_cursor(str(current_count))
        yield RunRequest(
            run_key=f"crypto_ingest_{current_count}",
            run_config={},
        )
