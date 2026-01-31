from .pull_clean_save import pull_data_from_source, clean_data, save_data_to_postgres_db
from .set_db_tables import prepare_postgres_tables
from .assets_to_fail import pull_data_from_other_source, show_stack_trace_for_returning_wrong_type, do_not_clean_data, do_other_operation

pull_clean_save_assets = [pull_data_from_source, clean_data, save_data_to_postgres_db]
prepare_postgres_tables_assets = [prepare_postgres_tables]
assets_to_fail_assets = [pull_data_from_other_source, show_stack_trace_for_returning_wrong_type, do_not_clean_data, do_other_operation]

all_assets = [*pull_clean_save_assets, *prepare_postgres_tables_assets, *assets_to_fail_assets]