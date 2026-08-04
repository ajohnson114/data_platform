from .data_pipeline_checks import check_no_nulls_in_required_columns, check_schema_matches_etl_table
from .model_checks import check_holdout_gap_reasonable, check_holdout_rmse_within_threshold

data_pipeline_asset_checks = [check_no_nulls_in_required_columns,
                              check_schema_matches_etl_table]

# Checks on what the model measured, rather than on the data it was given.
model_asset_checks = [check_holdout_rmse_within_threshold,
                      check_holdout_gap_reasonable]

all_asset_checks = [*data_pipeline_asset_checks, *model_asset_checks]
