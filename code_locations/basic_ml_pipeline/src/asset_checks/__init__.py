from .data_pipeline_checks import check_no_nulls_in_required_columns, check_schema_matches_etl_table

data_pipeline_asset_checks = [check_no_nulls_in_required_columns, 
                              check_schema_matches_etl_table]

all_asset_checks = [*data_pipeline_asset_checks]