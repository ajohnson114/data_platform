import pandas as pd
import numpy as np
from dagster import asset, MetadataValue
from config.config import get_config

@asset(group_name="etl_pipeline")
def pull_data_from_source(context) -> pd.DataFrame:
    """
    This is supposed to simulate pulling data from a source but it generates data.
    An acutal example of pulling data will be in the ml_pipeline group
    """
    num_rows = 100
    num_cols = 4
    column_names = [f"x_{i+1}" for i in range(num_cols)]

    data = np.random.rand(num_rows, num_cols)
    betas = np.array([10, 100, 1000, 0.5])
    alpha = 2.2

    df = pd.DataFrame(data, columns=column_names)
    df["y"] = data @ betas + alpha

    # Pollute data
    nan_rows = pd.DataFrame(np.nan, index=range(5), columns=df.columns)
    df = pd.concat([df, nan_rows], ignore_index=True)

    duplicate_rows = df.sample(n=3, replace=True)
    df = pd.concat([df, duplicate_rows], ignore_index=True)

    context.add_output_metadata(
        {
            "num_rows": len(df),
            "num_cols": len(df.columns),
            "head": MetadataValue.md(df.head().to_markdown(index=False)),
        }
    )

    return df



@asset(group_name="etl_pipeline")
def clean_data(context, pull_data_from_source: pd.DataFrame) -> pd.DataFrame:
    df = pull_data_from_source.drop_duplicates()
    df = df.dropna()

    context.add_output_metadata({
        "rows_after_cleaning": len(df),
        "dropped_rows": len(pull_data_from_source) - len(df),
    })

    return df


# -------------------------
# Persist to Postgres
# -------------------------

@asset(group_name="etl_pipeline",required_resource_keys={"etl_postgres"},deps=["clean_data", "prepare_postgres_tables"])
def save_data_to_postgres_db(context, clean_data: pd.DataFrame):
    table_name = get_config().get_etl_table_name()
    etl_postgres = context.resources.etl_postgres

    #This gives atomic writes and an auto roll back feature
    with etl_postgres.get_engine().begin() as conn:
        clean_data.to_sql(
            table_name,
            con=conn,
            if_exists="append",
            index=False,
            method="multi",
        )

    # Optional metadata after write
    context.add_output_metadata(
        {
            "table": table_name,
            "rows_inserted": len(clean_data),
        }
    )
