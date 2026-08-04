import json
import pickle
from datetime import datetime, timezone

import pandas as pd
from dagster import MetadataValue, asset
from sqlalchemy import text

from config.config import get_config

from ..model import artifact_name, train_holdout


@asset(group_name='ml_pipeline', required_resource_keys={"clickhouse"}, kinds={"clickhouse"})
def pull_data_from_warehouse(context) -> pd.DataFrame:
    """Read the snapshot the etl pipeline landed in the ClickHouse warehouse."""
    clickhouse = context.resources.clickhouse

    df = clickhouse.query_df(get_config().get_read_from_etl_table())

    context.add_output_metadata({
        "num_rows": len(df),
        "num_cols": len(df.columns),
        "head": MetadataValue.md(df.head().to_markdown(index=False)),
        "source_table": f"{clickhouse.database}.etl_table_snapshot",
    })

    return df


@asset(group_name='ml_pipeline', kinds={"scikitlearn"})
def fit_model(context, pull_data_from_warehouse: pd.DataFrame) -> dict:
    """Fit a linear model on a training split and score it on a holdout.

    The modelling itself is in src/model.py so it can be tested without a
    Dagster context; this asset is the orchestration around it -- read config,
    call it, publish what it measured.

    The estimator crosses to save_model through the io manager, which is the one
    thing the io manager is for: carrying an intermediate value between two
    steps of the same run. It is not where the model is *kept* -- save_model
    puts the durable copy in the landing zone, which is fs locally and s3 on
    EKS. A pickle in the io manager's storage is transport that happens to
    persist, not a model registry, and treating it as one is how you end up
    unable to say which artifact is serving.
    """
    model_config = get_config().get_model_config()

    result = train_holdout(
        pull_data_from_warehouse,
        feature_prefix=model_config['feature_prefix'],
        target_column=model_config['target_column'],
        test_size=float(model_config['test_size']),
        random_state=int(model_config['random_state']),
    )

    context.add_output_metadata({
        # Holdout first, and named so nobody has to guess which split it came
        # from. This is the number the asset check gates on.
        "holdout_rmse": result.holdout_rmse,
        "train_rmse": result.train_rmse,
        # The gap is the signal. On this generator both land near the noise
        # sigma; a train_rmse far below the holdout would mean memorisation.
        "rmse_gap": result.holdout_rmse - result.train_rmse,
        "holdout_r2": result.r2,
        "n_train": result.n_train,
        "n_test": result.n_test,
        "features": MetadataValue.json(result.features),
        # strict=True because a silent zip truncation here would not look like a
        # bug: it would publish a coefficient map that is simply missing its
        # last feature, or worse, labels shifted against values.
        "coefficients": MetadataValue.json(
            dict(zip(result.features, result.model.coef_.tolist(), strict=True))
        ),
        # float() because Dagster's metadata rejects numpy scalars.
        "intercept": float(result.model.intercept_),
    })

    return {
        "model": result.model,
        "features": result.features,
        "holdout_rmse": result.holdout_rmse,
        "train_rmse": result.train_rmse,
        "r2": result.r2,
        "n_train": result.n_train,
        "n_test": result.n_test,
    }


@asset(
    group_name='ml_pipeline',
    required_resource_keys={"ml_postgres", "landing_zone"},
    kinds={"postgres"},
)
def save_model(context, fit_model: dict) -> None:
    """Persist the trained model, then register it with the metrics it earned.

    Two stores, one for each half of the question "which model is this and how
    good was it": the artifact goes to the landing zone (a local directory under
    compose, S3 on EKS, chosen by the same config block the Parquet datasets
    use) and the row goes to Postgres pointing at it.

    An earlier version of this asset had the write commented out and inserted a
    path to a file that was never created -- a registry whose model_path column
    referred to nothing, which is worse than no registry, because it looks like
    one. The artifact is written BEFORE the row is inserted for that reason: a
    crash between the two leaves an unreferenced blob, which is inert, rather
    than a row pointing at a model that does not exist.
    """
    landing = context.resources.landing_zone
    ml_postgres = context.resources.ml_postgres

    trained_at = datetime.now(timezone.utc)
    features = fit_model["features"]

    # The features ride with the model in the same pickle. A model is unusable
    # without knowing which columns, in which order, produced its coefficients,
    # and storing that separately is an invitation for the two to drift.
    payload = pickle.dumps({"model": fit_model["model"], "features": features})
    model_uri = landing.put_artifact(
        get_config().get_model_artifact_dataset(),
        artifact_name(trained_at, run_id=context.run_id),
        payload,
    )

    with ml_postgres.get_engine().begin() as conn:
        conn.execute(
            text(get_config().get_ml_insert_statement().format(
                get_config().get_ml_table_name()
            )),
            {
                "model_path": model_uri,
                # `rmse` is the holdout number. The column predates the split
                # and kept its name so existing rows stay readable; what changed
                # is that it now measures data the model had not seen.
                "rmse": float(fit_model["holdout_rmse"]),
                "train_rmse": float(fit_model["train_rmse"]),
                "r2": float(fit_model["r2"]),
                "n_train": int(fit_model["n_train"]),
                "n_test": int(fit_model["n_test"]),
                "features": json.dumps(features),
                "trained_at": trained_at,
            },
        )

    context.add_output_metadata({
        "model_uri": MetadataValue.path(model_uri),
        "artifact_bytes": len(payload),
        "holdout_rmse": float(fit_model["holdout_rmse"]),
        "train_rmse": float(fit_model["train_rmse"]),
        "holdout_r2": float(fit_model["r2"]),
        "features": MetadataValue.json(features),
        "trained_at": trained_at.isoformat(),
        "registry_table": get_config().get_ml_table_name(),
    })
