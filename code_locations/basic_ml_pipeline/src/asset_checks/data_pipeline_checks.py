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

@asset_check(asset="pull_data_from_postgres", name="schema_matches_etl_table", blocking=True)
def check_schema_matches_etl_table(pull_data_from_postgres: pd.DataFrame) -> AssetCheckResult:
    if len(pull_data_from_postgres) == 0:
        return AssetCheckResult(
            passed=False,
            metadata={"reason": "DataFrame is empty — run etl_job first"}
        )
    
    df = pull_data_from_postgres
    expected_schema = {
        col: TYPE_MAPPING[spec["type"]]
        for col, spec in get_config().get_expected_schema_from_etl_pipeline().items()
    }

    missing_columns = []
    wrong_type_columns = {}

    for col, expected_dtype in expected_schema.items():
        if col not in df.columns:
            missing_columns.append(col)
        else:
            if not np.issubdtype(df[col].dtype, expected_dtype):
                wrong_type_columns[col] = str(df[col].dtype)

    passed = not missing_columns and not wrong_type_columns
    return AssetCheckResult(
        passed=passed,
        metadata={
            "missing_columns": missing_columns,
            "columns_with_wrong_dtype": wrong_type_columns,
            "observed_dtypes": {col: str(df[col].dtype) for col in df.columns},
        },
    )

@asset_check(asset="pull_data_from_postgres", name="no_nulls_in_required_columns", blocking=True)
def check_no_nulls_in_required_columns(pull_data_from_postgres: pd.DataFrame) -> AssetCheckResult:
    if len(pull_data_from_postgres) == 0:
        return AssetCheckResult(
            passed=False,
            metadata={"reason": "DataFrame is empty — run etl_job first"}
        )
    
    df = pull_data_from_postgres
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