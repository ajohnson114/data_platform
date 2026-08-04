"""
Checks on what the model measured, as opposed to what it was fed.

The two checks in data_pipeline_checks.py guard the ETL team's output on the way
in -- schema and nulls, both blocking, both about a contract between two teams.
These guard the model on the way out, and they are deliberately not the same
kind of check as each other, for the same reason the streaming checks aren't:
treating everything as a gate is how `blocking` stops meaning anything.
"""
from dagster import AssetCheckResult, AssetCheckSeverity, asset_check

from config.config import get_config


@asset_check(asset="fit_model", name="holdout_rmse_within_threshold", blocking=True)
def check_holdout_rmse_within_threshold(fit_model: dict) -> AssetCheckResult:
    """
    Blocking, and the gate is real: a model worse than this should not be
    registered as the current one.

    save_model is downstream, so a failure here stops the artifact being written
    and the registry row being inserted. That is the whole point -- the failure
    mode being prevented is a bad model quietly replacing a good one in a table
    that something else reads to decide what to serve.

    The threshold is compared against HOLDOUT error, which is the only reason it
    can mean anything. Against in-sample error this check would pass on any
    model that had memorised its training data.
    """
    threshold = get_config().get_max_holdout_rmse()
    holdout_rmse = float(fit_model["holdout_rmse"])

    return AssetCheckResult(
        passed=holdout_rmse <= threshold,
        metadata={
            "holdout_rmse": holdout_rmse,
            "max_holdout_rmse": threshold,
            "train_rmse": float(fit_model["train_rmse"]),
            "holdout_r2": float(fit_model["r2"]),
            "n_test": int(fit_model["n_test"]),
        },
    )


@asset_check(asset="fit_model", name="holdout_gap_reasonable", blocking=False)
def check_holdout_gap_reasonable(fit_model: dict) -> AssetCheckResult:
    """
    Advisory, and fails WARN. A holdout error much worse than the training error
    means the model has fit noise rather than signal -- worth surfacing, not
    worth halting on.

    Not blocking because the number that decides whether a model is fit to
    register is already checked above. A model can generalise poorly relative to
    its training fit and still be comfortably inside the RMSE budget, and
    refusing to register it in that case would be the check overriding the
    threshold someone deliberately set.

    Expressed as a ratio rather than a difference so it does not have to be
    retuned every time the target's scale changes.
    """
    train_rmse = float(fit_model["train_rmse"])
    holdout_rmse = float(fit_model["holdout_rmse"])

    # A perfect training fit makes the ratio infinite and says nothing useful --
    # on a target with real noise it does not happen, and if it does the
    # interesting fact is the zero, not the ratio.
    if train_rmse == 0:
        return AssetCheckResult(
            passed=holdout_rmse == 0,
            severity=AssetCheckSeverity.WARN,
            metadata={
                "reason": "train_rmse is 0; nothing to compare a ratio against",
                "holdout_rmse": holdout_rmse,
            },
        )

    ratio = holdout_rmse / train_rmse
    return AssetCheckResult(
        # 1.5x is generous on purpose. With a 20% holdout the two splits differ
        # by sampling alone, and a bound tight enough to catch mild variance
        # would fire on every ordinary run.
        passed=ratio <= 1.5,
        severity=AssetCheckSeverity.WARN,
        metadata={
            "holdout_over_train_rmse": ratio,
            "train_rmse": train_rmse,
            "holdout_rmse": holdout_rmse,
        },
    )
