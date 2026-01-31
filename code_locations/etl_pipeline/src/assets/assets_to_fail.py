import pandas as pd
import numpy as np
from dagster import asset, MetadataValue

# Making this file to show the case of assets failing

@asset(group_name="failing_pipeline")
def pull_data_from_other_source(context) -> pd.DataFrame:
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

@asset(group_name="failing_pipeline", deps=["pull_data_from_other_source"])
def show_stack_trace_for_returning_wrong_type(context) -> None:
    return 'Hello! Thanks for checking out the project!'


@asset(group_name="failing_pipeline")
def do_not_clean_data(context, pull_data_from_other_source: pd.DataFrame) -> pd.DataFrame:
    context.add_output_metadata({
        "num_null_rows": int(pull_data_from_other_source.isnull().any(axis=1).sum()),
    })

    return pull_data_from_other_source


@asset(group_name="failing_pipeline", deps=['do_not_clean_data'])
def do_other_operation(context) -> None:
    """This will never run!"""
    pass

