from dagster import asset_check, AssetCheckResult
import pandas as pd
import numpy as np
from config.config import get_config

# YAML won't serialize python objects so I have to put this here
TYPE_MAPPING = {
    "float": np.floating,
    "int": np.integer,
    "string": object,
}

@asset_check(asset="do_not_clean_data", name="no_nulls_in_required_columns_failing_pipeline", blocking=True)
def check_no_nulls_in_required_columns_failing_pipeline(do_not_clean_data: pd.DataFrame) -> AssetCheckResult:
    df = do_not_clean_data

    columns_to_check = get_config().get_cols_required_to_not_have_nulls()
    null_counts = df[columns_to_check].isna().sum()
    total_nulls = int(null_counts.sum())

    passed = total_nulls == 0

    return AssetCheckResult(
        passed=passed,
        metadata={
            "total_rows": len(df),
            "total_nulls": total_nulls,
            "nulls_by_column": null_counts.to_dict(),
            "columns_checked": columns_to_check
        },
    )
