from .pull_clean_save import pull_data_from_source, clean_data, save_data_to_postgres_db
from .set_db_tables import prepare_postgres_tables
from .assets_to_fail import pull_data_from_other_source, show_stack_trace_for_returning_wrong_type, do_not_clean_data, do_other_operation
from .bsky_to_warehouse import bsky_records_landed, bsky_records_snapshot
from .purge_warehouse import purge_deleted_records
from .purge_landing import purge_landing_archive
from .etl_to_warehouse import etl_table_landed, etl_table_snapshot
from ..dbt import dbt_analytics_assets

pull_clean_save_assets = [pull_data_from_source, clean_data, save_data_to_postgres_db]
prepare_postgres_tables_assets = [prepare_postgres_tables]
assets_to_fail_assets = [pull_data_from_other_source, show_stack_trace_for_returning_wrong_type, do_not_clean_data, do_other_operation]
streaming_ingest_assets = [bsky_records_landed, bsky_records_snapshot, purge_deleted_records, purge_landing_archive]
etl_to_warehouse_assets = [etl_table_landed, etl_table_snapshot]
# One object covering every model in the dbt project; Dagster expands it into
# an asset per node from the manifest.
dbt_assets_list = [dbt_analytics_assets]

all_assets = [*pull_clean_save_assets, *prepare_postgres_tables_assets, *assets_to_fail_assets, *streaming_ingest_assets, *etl_to_warehouse_assets, *dbt_assets_list]
