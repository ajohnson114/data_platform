"""
Training, splitting and metrics -- the part of the ml pipeline that is not Dagster.

Separated from the assets for the same reason warehouse_targets.py is separated
from the assets that read it: everything here is a pure function of a DataFrame
and a few config values, so it can be tested without a Dagster context, a
warehouse, or a running platform. The assets in assets/train_lr.py are then thin
enough to read as orchestration rather than as modelling.

WHY THERE IS A HOLDOUT AT ALL.
An earlier version fit the model and then called predict() on the same rows,
reporting that error as the model's RMSE. On data whose target is an exact
linear function of its features that number is ~0 by construction, and it stays
~0 no matter how badly the model would do on anything it had not already seen.
It measured arithmetic, not generalisation. The split is what makes the number
in the registry -- and the threshold the asset check gates on -- mean something.
"""
import math
from collections import namedtuple

import numpy as np
from sklearn.linear_model import LinearRegression
from sklearn.metrics import mean_squared_error, r2_score
from sklearn.model_selection import train_test_split

# holdout_rmse first because it is the number that matters: it is what the asset
# check gates on and what goes into the registry's `rmse` column. train_rmse
# rides along beside it so the gap between the two is visible -- a model whose
# training error is far below its holdout error has memorised rather than
# learned, and that is only detectable if both are recorded.
TrainingResult = namedtuple(
    "TrainingResult",
    "model features holdout_rmse train_rmse r2 n_train n_test",
)


def select_features(df, prefix: str) -> list:
    """
    Feature columns, by prefix, in a stable order.

    Sorted rather than left in DataFrame order because the list is persisted
    alongside the model and used to align columns at predict time. Column order
    out of a warehouse SELECT * is not a contract, and a model whose
    coefficients silently transpose because two columns swapped is the kind of
    bug that produces plausible numbers forever.

    Raises rather than returning an empty list: sklearn's own error for a
    zero-column matrix names neither the prefix nor the columns that were
    actually present, and this runs inside a container where that context is
    the whole diagnosis.
    """
    features = sorted(c for c in df.columns if c.startswith(prefix))
    if not features:
        raise ValueError(
            f"no feature columns matching prefix {prefix!r}; "
            f"the frame has {sorted(df.columns)}"
        )
    return features


def train_holdout(df, *, feature_prefix: str, target_column: str,
                  test_size: float, random_state: int) -> TrainingResult:
    """
    Fit on a training split, score on a holdout the model never saw.

    `random_state` is fixed from config rather than left to chance so that the
    same data produces the same split and therefore the same holdout RMSE. The
    asset check gates on that number, and a threshold measured against a
    different subset every run would make a marginal model pass or fail at
    random -- an asset check that flaps is one people learn to ignore.
    """
    if target_column not in df.columns:
        raise ValueError(
            f"target column {target_column!r} is not in the frame; "
            f"it has {sorted(df.columns)}"
        )

    # The target is removed explicitly rather than assumed not to match the
    # prefix. If it ever does -- a rename to `y_actual` with prefix `y_` is all
    # it takes -- the model receives its own answer as an input, holdout RMSE
    # goes to 0, r2 to 1, and every gate downstream passes on a model that has
    # learned nothing. A confident number with nothing about it that looks
    # wrong is the failure mode this module is arranged to avoid.
    features = [c for c in select_features(df, feature_prefix) if c != target_column]
    if not features:
        raise ValueError(
            f"prefix {feature_prefix!r} matched only the target column "
            f"{target_column!r}; there are no features to train on"
        )

    X, y = df[features], df[target_column]

    # Two guards, because there are two ways to end up with a metric that is a
    # number but not a measurement, and they fail independently.

    # Not enough data to be modelling at all. Cheap tripwire for an empty or
    # half-loaded warehouse table, which is the realistic cause.
    if len(df) < 10:
        raise ValueError(
            f"refusing to train on {len(df)} rows: too few to fit and evaluate "
            f"anything meaningful"
        )

    # Enough rows, but the split leaves nothing to measure ON. An RMSE over a
    # single row is one draw from the noise distribution, and it is what the
    # blocking asset check compares against its threshold -- so a lucky residual
    # would pass the gate no matter how bad the model is. r2 is undefined below
    # two points and returns NaN, which then lands in the registry.
    #
    # ceil, because that is how train_test_split sizes a fractional split. A
    # guard that approximates the thing it guards is a guard with a gap.
    n_test = math.ceil(test_size * len(df))
    if n_test < 2:
        raise ValueError(
            f"refusing to train: a test_size of {test_size:g} over {len(df)} "
            f"rows leaves a holdout of {n_test}, too small for its RMSE to "
            f"mean anything"
        )

    X_train, X_test, y_train, y_test = train_test_split(
        X, y, test_size=test_size, random_state=random_state
    )

    model = LinearRegression()
    model.fit(X_train, y_train)

    # np.sqrt(mean_squared_error(...)) rather than root_mean_squared_error or
    # squared=False: the first was added in sklearn 1.4 and the second removed
    # in 1.6, and the code locations pin neither.
    train_rmse = float(np.sqrt(mean_squared_error(y_train, model.predict(X_train))))
    holdout_predictions = model.predict(X_test)
    holdout_rmse = float(np.sqrt(mean_squared_error(y_test, holdout_predictions)))

    return TrainingResult(
        model=model,
        features=features,
        holdout_rmse=holdout_rmse,
        train_rmse=train_rmse,
        r2=float(r2_score(y_test, holdout_predictions)),
        n_train=int(len(X_train)),
        n_test=int(len(X_test)),
    )


def artifact_name(trained_at, run_id: str = None, prefix: str = "linear_model") -> str:
    """
    The file name a trained model is stored under.

    Same stamp format the landing zone uses for Parquet batches
    (`%Y%m%dT%H%M%SZ`), so a listing of the models prefix sorts chronologically
    for the same reason a listing of a dataset does -- and so there is one
    timestamp convention in the repo rather than two.

    The run id is appended because the stamp resolves only to the second and
    both storage backends overwrite silently (os.replace, put_object). Two runs
    starting within the same second -- a double-clicked materialise is enough,
    and the instance allows ten concurrent -- would otherwise write the same
    key, leaving two registry rows pointing at one artifact and no way to tell
    which model each row describes. A landed Parquet batch does not need this
    because its name carries an id range that cannot collide.

    Kept optional so the name is still derivable outside a run, which is what
    makes it testable without a Dagster context.
    """
    stamp = trained_at.strftime('%Y%m%dT%H%M%SZ')
    suffix = f"__{run_id}" if run_id else ""
    return f"{prefix}__{stamp}{suffix}.pkl"
