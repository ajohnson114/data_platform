import pandas as pd
import numpy as np
from dagster import asset, MetadataValue
from sklearn.linear_model import LinearRegression
from sklearn.metrics import mean_squared_error
from sqlalchemy import text
import json
from datetime import datetime
import os

from config.config import get_config

@asset(group_name='ml_pipeline',required_resource_keys={"ml_postgres"})
def pull_data_from_postgres(context) -> pd.DataFrame:
    table_name = get_config().get_etl_table_name()
    ml_postgres = context.resources.ml_postgres

    with ml_postgres.get_engine().begin() as conn:
        df = pd.read_sql(text(get_config().get_read_from_etl_table()), conn)

    context.add_output_metadata({
        "num_rows": len(df),
        "num_cols": len(df.columns),
        "head": MetadataValue.md(df.head().to_markdown(index=False)),
        "source_table": table_name
    })

    return df

@asset(group_name='ml_pipeline')
def fit_model(context, pull_data_from_postgres: pd.DataFrame) -> dict:
    df = pull_data_from_postgres
    X = df[[col for col in df.columns if col.startswith("x_")]]
    y = df["y"]

    model = LinearRegression()
    model.fit(X, y)
    predictions = model.predict(X)
    rmse = np.sqrt(mean_squared_error(y, predictions))

    context.add_output_metadata({
        "num_training_rows": len(df),
        "coefficients": MetadataValue.json(model.coef_.tolist()),
        "intercept": float(model.intercept_),
        "rmse": float(rmse) #numpy isn't accepted in this apparently
    })

    return {"model": model, "features": X.columns.tolist(), "rmse": rmse}

@asset(group_name='ml_pipeline',required_resource_keys={"ml_postgres"})
def save_model(context, fit_model: dict):
    # model = fit_model["model"]
    features = fit_model["features"]
    rmse = fit_model["rmse"]

    # --- Pretending to save model locally (Mock S3) ---
    timestamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    save_dir = "./saved_models"
    # os.makedirs(save_dir, exist_ok=True)
    model_filename = f"linear_model_{timestamp}.pkl"
    model_path = os.path.join(save_dir, model_filename)

    # with open(model_path, "wb") as f:
    #     pickle.dump({"model": model, "features": features}, f)

    # --- Record metadata in Postgres ---
    table_name = get_config().get_ml_table_name()
    ml_postgres = context.resources.ml_postgres

    with ml_postgres.get_engine().begin() as conn:
        conn.execute(
            text(get_config().get_ml_insert_statement().format(table_name)),
            {
                "model_path": model_path,
                "rmse": float(rmse),
                "features": json.dumps(features)
            }
        )

    context.add_output_metadata({
        "model_path": model_path,
        "features": MetadataValue.json(features),
        "rmse": float(rmse)
    })
