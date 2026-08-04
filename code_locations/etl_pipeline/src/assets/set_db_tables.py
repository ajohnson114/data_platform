from dagster import asset
from sqlalchemy import text
from config.config import get_config

@asset(group_name='db_setup',required_resource_keys={"etl_postgres"}, kinds={"postgres"})
def prepare_postgres_tables(context) -> None:
    etl_postgres = context.resources.etl_postgres

    with etl_postgres.get_engine().begin() as conn:
        context.log.info("Starting to make the etl table")
        conn.execute(text(get_config().get_etl_table_ddl()))
        context.log.info('Finished making the etl table')

        context.log.info("Starting to make the ml table")
        conn.execute(text(get_config().get_ml_table_ddl()))
        # The CREATE above is a no-op against a volume that already has the
        # table, so the metric columns arrive by ALTER. Both statements are
        # idempotent, which is what makes this safe to run on every materialise.
        for alter in get_config().get_ml_table_alters():
            conn.execute(text(alter))
        context.log.info('Finished making the ml table')

    context.log.info("Postgres schema ensured")
