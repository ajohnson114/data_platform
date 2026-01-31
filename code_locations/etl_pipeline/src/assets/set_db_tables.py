from dagster import asset
from sqlalchemy import text
from config.config import get_config

@asset(group_name='db_setup',required_resource_keys={"etl_postgres"})
def prepare_postgres_tables(context):
    etl_postgres = context.resources.etl_postgres

    with etl_postgres.get_engine().begin() as conn:
        context.log.info("Starting to make the etl table")
        conn.execute(text(get_config().get_etl_table_ddl()))
        context.log.info('Finished making the etl table')

        context.log.info("Starting to make the ml table")
        conn.execute(text(get_config().get_ml_table_ddl()))
        context.log.info('Finished making the ml table')

    context.log.info("Postgres schema ensured")
