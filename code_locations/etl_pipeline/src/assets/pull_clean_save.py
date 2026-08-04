import pandas as pd
import numpy as np
from dagster import asset, MetadataValue
from config.config import get_config

@asset(group_name="etl_pipeline")
def pull_data_from_source(context) -> pd.DataFrame:
    """
    Stands in for pulling from a source system; it generates the data instead.
    An actual example of pulling from a source is in the ml_pipeline group.

    Shape comes from config because the ml pipeline's holdout depends on it: at
    the original 100 rows a 20% test split was 20 points, and its RMSE moved by
    several percent from one run to the next -- enough to make the model's asset
    check flap rather than catch anything.

    Deliberately NOT seeded. Each materialisation appends a fresh sample to
    etl_table, which is what makes re-running the job produce a growing dataset
    rather than the same rows over and over. The split downstream is seeded; the
    source is not, and those are different decisions.
    """
    source = get_config().get_source_config()
    num_rows = int(source["num_rows"])
    noise_sigma = float(source["noise_sigma"])

    # The width comes from the coefficients, not from config: etl_table's DDL
    # declares x_1..x_4 and the ml pipeline's schema check asserts the same
    # four, so the shape is already pinned twice and a third place to set it
    # would only be a third place for it to disagree.
    betas = np.array([10, 100, 1000, 0.5])
    alpha = 2.2
    num_cols = len(betas)
    column_names = [f"x_{i+1}" for i in range(num_cols)]

    data = np.random.rand(num_rows, num_cols)

    df = pd.DataFrame(data, columns=column_names)
    # Gaussian noise on the target, so `y` is not an exact function of `x`.
    # Without it a linear fit is perfect, holdout RMSE lands around 1e-13, and
    # the model asset check measures floating point rather than the model. With
    # it, noise_sigma is the floor no model can beat and the check has something
    # real to compare against.
    df["y"] = data @ betas + alpha + np.random.normal(0, noise_sigma, num_rows)

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

@asset(group_name="etl_pipeline",required_resource_keys={"etl_postgres"},deps=["clean_data", "prepare_postgres_tables"], kinds={"postgres"})
def save_data_to_postgres_db(context, clean_data: pd.DataFrame) -> None:
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
