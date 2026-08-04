from dagster import sensor, RunRequest, SkipReason, DefaultSensorStatus, SensorEvaluationContext
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
def bsky_record_sensor(context: SensorEvaluationContext):
    """Sensor that detects new Bluesky change events in Postgres and triggers warehouse ingest."""
    table_name = get_config().get_bsky_records_table_name()

    # Deliberately unguarded below this point. This used to wrap the whole query
    # in `except Exception: log.warning(); return`, which made an unreachable
    # Postgres indistinguishable from "no new data": ingestion stopped, no run
    # was requested, and nothing surfaced it. An uncaught exception marks the
    # tick failed in the Dagster UI, which is what an alert can be hung off. The
    # daemon keeps evaluating the sensor afterwards, so a transient outage still
    # self-heals -- it just does so visibly.
    #
    # The one genuinely expected absence is the table itself: spark_consumer
    # creates bsky_records on its first batch, so on a cold `make` the sensor runs
    # before there is anything to look at. That is a skip, not a failure, and is
    # checked explicitly so it can't mask a real connection error. to_regclass
    # returns NULL instead of raising for a missing relation.
    with _db_client.get_engine().begin() as conn:
        exists = conn.execute(
            text("SELECT to_regclass(:table)"), {"table": table_name}
        ).scalar()
        if exists is None:
            yield SkipReason(
                f"{table_name} does not exist yet -- waiting for spark_consumer "
                f"to write its first batch."
            )
            return

        # MAX(id) rather than COUNT(*): the id is the primary key, so this is an
        # index lookup no matter how large the table is. COUNT(*) is a sequential
        # scan in Postgres, which was free against a table growing by five rows a
        # minute and is not against a firehose.
        result = conn.execute(text(f"SELECT MAX(id) FROM {table_name}")).fetchone()
        current_max = result[0] if result and result[0] is not None else 0

    last_max = int(context.cursor) if context.cursor else 0

    if current_max > last_max:
        context.log.info(f"New events detected: {last_max} -> {current_max}")
        context.update_cursor(str(current_max))
        yield RunRequest(
            run_key=f"bsky_ingest_{current_max}",
            run_config={},
        )
