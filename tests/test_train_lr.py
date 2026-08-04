"""
Unit tests for the ml pipeline's Dagster-free modelling core.

The claim the module exists to make is that the holdout is genuinely held out:
the number the asset check gates on and the registry stores has to measure
generalisation rather than arithmetic. Most of what follows is that claim,
driven by a generator matching the ETL's own so the error the model earns can be
compared against the noise floor the data was built with.

The artifact side of the landing zone is here rather than in test_landing_zone.py
because it exists for this pipeline, and because the property that matters --
that a model pickle cannot reach the extract's watermark -- is only interesting
with both halves in the same test.
"""
import pickle
from datetime import datetime

import numpy as np
import pandas as pd
import pytest
import yaml

from conftest import REPO_ROOT, load_module

model = load_module("code_locations/basic_ml_pipeline/src/model.py", name="ml_model")
landing_zone = load_module("code_locations/shared/resources/landing_zone.py")

# The generator in etl_pipeline/src/assets/pull_clean_save.py, restated. y is an
# exact linear function of four uniform features plus gaussian noise, so the
# noise sigma is the floor no correctly specified model can beat.
BETAS = np.array([10.0, 100.0, 1000.0, 0.5])
ALPHA = 2.2
FEATURES = ["x_1", "x_2", "x_3", "x_4"]

DATASET = "etl_table"
ARTIFACT_DATASET = "models"
STAMP = datetime(2026, 8, 1, 16, 42, 58)

ML_CONFIG_DIR = REPO_ROOT / "code_locations/basic_ml_pipeline/config/config"
ETL_CONFIG_DIR = REPO_ROOT / "code_locations/etl_pipeline/config/config"
ML_ENVS = ["dev", "uat", "prod", "aws"]

# Every key fit_model() reads out of get_model_config().
MODEL_KEYS = {"feature_prefix", "target_column", "test_size", "random_state", "artifact_dataset"}


def _generated(num_rows: int = 2000, noise_sigma: float = 2.0, seed: int = 0) -> pd.DataFrame:
    """The ETL's frame, seeded -- a flaky metric test is worse than no metric test."""
    rng = np.random.default_rng(seed)
    data = rng.random((num_rows, len(BETAS)))
    df = pd.DataFrame(data, columns=FEATURES)
    df["y"] = data @ BETAS + ALPHA + rng.normal(0, noise_sigma, num_rows)
    return df


def _train(df: pd.DataFrame, **overrides):
    """train_holdout with the dev config's arguments, overridable per test."""
    kwargs = {"feature_prefix": "x_", "target_column": "y", "test_size": 0.2, "random_state": 42}
    return model.train_holdout(df, **{**kwargs, **overrides})


def _landed_frame(start_id: int, end_id: int) -> pd.DataFrame:
    ids = list(range(start_id, end_id + 1))
    return pd.DataFrame({"id": ids, "text": [f"row-{i}" for i in ids]})


def _yaml(path):
    with open(path) as handle:
        return yaml.safe_load(handle)["data_pipeline"]


def _ml_config(env: str = "dev") -> dict:
    return _yaml(ML_CONFIG_DIR / f"config.{env}.yaml")


@pytest.fixture
def zone(tmp_path):
    return landing_zone.LocalLandingZone(base_dir=str(tmp_path))


# --- feature selection -------------------------------------------------------

def test_select_features_returns_matching_columns_in_sorted_order():
    df = pd.DataFrame(columns=["x_3", "x_1", "x_10", "x_2", "y"])

    # Sorted, not DataFrame order: the list is persisted with the model and used
    # to align columns at predict time, so what matters is that it is the same
    # every run. It is lexicographic, so x_10 lands before x_2 -- harmless at the
    # four features the DDL declares, and self-consistent at any width because
    # fit and predict both read this list. It only becomes surprising to a human
    # reading the registry's `features` column once the count reaches double
    # digits; the fix then is a natural sort here, not DataFrame order.
    assert model.select_features(df, "x_") == ["x_1", "x_10", "x_2", "x_3"]


def test_select_features_ignores_the_target_and_the_warehouse_provenance_columns():
    # _source_file and _landed_at are stamped by the warehouse loader and arrive
    # on every row of the SELECT * the ml pipeline reads.
    df = pd.DataFrame(columns=["x_1", "x_2", "y", "_source_file", "_landed_at", "id"])

    assert model.select_features(df, "x_") == ["x_1", "x_2"]


def test_select_features_names_the_prefix_and_the_columns_it_actually_saw():
    df = pd.DataFrame(columns=["a", "b", "y"])

    with pytest.raises(ValueError) as excinfo:
        model.select_features(df, "x_")

    # This runs in a container where the message is the whole diagnosis, so both
    # halves of "what was asked for" and "what was there" have to be in it.
    message = str(excinfo.value)
    assert "'x_'" in message
    assert "['a', 'b', 'y']" in message


# --- refusals ----------------------------------------------------------------

def test_train_holdout_names_the_target_column_that_is_missing():
    with pytest.raises(ValueError) as excinfo:
        _train(_generated(50), target_column="label")

    assert "'label'" in str(excinfo.value)


@pytest.mark.parametrize("num_rows", [1, 5, 9])
def test_train_holdout_refuses_a_frame_too_small_to_measure(num_rows):
    with pytest.raises(ValueError) as excinfo:
        _train(_generated(num_rows))

    # The row count is in the message because the caller's next question is
    # always "how few?", and a run that failed for want of data should not need
    # a second materialisation to answer it.
    assert f"{num_rows} rows" in str(excinfo.value)


# Regression: the first version of this guard counted frame rows, so a small
# test_size slipped a single-row holdout past it -- and that one residual is
# what the blocking asset check would have compared against its threshold.
def test_train_holdout_refuses_a_holdout_of_a_single_row():
    # model.py:80-87 says a one-row holdout is "one draw from the noise
    # distribution, which is a number but not a measurement", and that failing
    # here beats registering it -- then guards on len(df), which is not that
    # quantity. 20 rows at test_size 0.05 clears the guard and lands exactly
    # there: n_test is 1, holdout_rmse is a single residual, and r2 is nan.
    # Both go into the registry row and into the blocking check's metadata, and
    # a single residual can sit anywhere under the threshold however bad the
    # model is. Reachable by config alone -- test_size is a config value.
    with pytest.raises(ValueError):
        _train(_generated(20), test_size=0.05)


# --- the split ---------------------------------------------------------------

def test_split_sizes_account_for_every_row():
    result = _train(_generated(100))

    assert result.n_test == round(0.2 * 100) == 20
    assert result.n_train + result.n_test == 100


def test_the_same_random_state_reproduces_the_split_and_every_metric():
    df = _generated(500)

    first, second = _train(df), _train(df)

    # This is what the asset check's threshold rests on: the same data has to
    # produce the same holdout number, or a marginal model passes or fails at
    # random and people learn to ignore the check.
    assert (first.holdout_rmse, first.train_rmse, first.r2) == (
        second.holdout_rmse, second.train_rmse, second.r2
    )
    assert np.array_equal(first.model.coef_, second.model.coef_)
    assert (first.n_train, first.n_test) == (second.n_train, second.n_test)


def test_a_different_random_state_scores_a_different_holdout():
    df = _generated(500)

    # The other half of the determinism claim: random_state is doing something,
    # so the reproducibility above is the seed rather than an accident of the
    # data being uniform enough for any split to score the same.
    assert _train(df).holdout_rmse != _train(df, random_state=7).holdout_rmse


# --- what the holdout measures -----------------------------------------------

def test_holdout_rmse_lands_on_the_noise_floor_rather_than_zero():
    result = _train(_generated(2000, noise_sigma=2.0))

    # A correctly specified linear fit on 2000 rows can do no better than the
    # sigma the generator added, and should do no worse. 400 holdout rows put
    # the sampling spread of the RMSE estimate near sigma/sqrt(2*n_test) ~ 0.07,
    # so this band is several standard errors wide -- loose enough never to
    # flake, and nowhere near loose enough to be satisfied by the ~1e-13 an
    # in-sample metric reports on this generator.
    assert 1.5 < result.holdout_rmse < 2.6
    assert result.r2 > 0.99


def test_recovered_coefficients_are_close_to_the_true_betas():
    result = _train(_generated(2000, noise_sigma=2.0))

    # Not self-consistency: these are the numbers the generator used, so
    # recovering them means the split kept X and y aligned and the sorted
    # feature list addresses the columns it claims to. The standard error on
    # each coefficient is ~0.17 here, so the tolerance is deliberately loose.
    assert result.features == FEATURES
    assert np.allclose(result.model.coef_, BETAS, atol=1.0)
    assert abs(float(result.model.intercept_) - ALPHA) < 1.0


def test_noiseless_data_fits_exactly_which_is_what_the_old_metric_always_saw():
    result = _train(_generated(2000, noise_sigma=0.0))

    # Exactly why the ETL generator now adds noise. With y an exact linear
    # function of x both numbers are ~0, and a threshold compared against either
    # of them measures floating point rather than the model -- which is what the
    # in-sample metric this module replaced reported on every single run.
    assert result.train_rmse < 1e-8
    assert result.holdout_rmse < 1e-8


def test_train_holdout_does_not_mutate_the_frame_it_was_given():
    df = _generated(200)
    before = df.copy(deep=True)

    _train(df)

    pd.testing.assert_frame_equal(df, before)


def test_columns_that_are_not_features_never_reach_the_model():
    df = _generated(200)
    df["id"] = range(len(df))
    df["inserted_at"] = pd.Timestamp("2026-08-01")
    df["_source_file"] = "/app/landing/etl_table/x.parquet"

    result = _train(df)

    assert result.features == FEATURES
    assert len(result.model.coef_) == len(FEATURES)
    # And they change nothing about the fit: same metrics as the frame without
    # them, so a warehouse adding a column cannot silently move the registry's
    # numbers.
    assert result.holdout_rmse == pytest.approx(_train(_generated(200)).holdout_rmse)


# --- artifact naming ---------------------------------------------------------

@pytest.mark.parametrize("kwargs, expected", [
    ({}, "linear_model__20260801T164258Z.pkl"),
    ({"prefix": "ridge"}, "ridge__20260801T164258Z.pkl"),
])
def test_artifact_name_stamps_the_moment_the_model_was_trained(kwargs, expected):
    assert model.artifact_name(STAMP, **kwargs) == expected


def test_artifact_names_sort_chronologically_as_strings():
    moments = [
        datetime(2026, 8, 1, 9, 0, 0),
        datetime(2026, 8, 1, 16, 42, 58),
        datetime(2026, 12, 31, 23, 59, 59),
    ]

    names = [model.artifact_name(m) for m in moments]

    # A listing of the models prefix is a history, for the same reason a listing
    # of a dataset is: the stamp is fixed-width, so lexicographic is chronological.
    assert sorted(names) == names


def test_artifact_names_use_the_landing_zone_stamp_format():
    # One timestamp convention in the repo rather than two. Asserted against
    # _build_name rather than against the literal so the two cannot drift apart
    # silently -- a change to either format breaks this.
    stamp = STAMP.strftime(landing_zone._STAMP_FMT)

    assert landing_zone._STAMP_FMT == "%Y%m%dT%H%M%SZ"
    assert model.artifact_name(STAMP) == f"linear_model__{stamp}.pkl"
    assert landing_zone._build_name(DATASET, 1, 2, STAMP).endswith(f"__{stamp}.parquet")


# --- artifacts in the landing zone -------------------------------------------

def test_put_artifact_round_trips_the_exact_bytes_of_a_real_model_pickle(zone):
    result = _train(_generated(200))
    # The payload save_model actually writes: the estimator and the feature list
    # in one pickle, because a model is unusable without knowing its columns.
    payload = pickle.dumps({"model": result.model, "features": result.features})

    uri = zone.put_artifact(ARTIFACT_DATASET, model.artifact_name(STAMP), payload)

    assert zone.get_artifact(uri) == payload
    restored = pickle.loads(zone.get_artifact(uri))
    assert restored["features"] == result.features
    assert np.array_equal(restored["model"].coef_, result.model.coef_)


def test_put_artifact_overwrites_a_name_it_has_already_written(zone):
    name = model.artifact_name(STAMP)

    first = zone.put_artifact(ARTIFACT_DATASET, name, b"first")
    second = zone.put_artifact(ARTIFACT_DATASET, name, b"second")

    # Artifacts carry no id range, so none of the immutability the Parquet
    # protocol promises applies to them; the caller owns the name and therefore
    # owns whether two writes collide.
    assert first == second
    assert zone.get_artifact(second) == b"second"


def test_an_artifact_is_invisible_to_the_parquet_dataset_beside_it(zone):
    landed = zone.write(DATASET, _landed_frame(1, 100), STAMP)
    watermark = zone.max_landed_id(DATASET)

    zone.put_artifact(ARTIFACT_DATASET, model.artifact_name(STAMP), b"a model pickle")

    # The separation the module docstring calls load-bearing: the extract's only
    # bookmark is max(end_id) over this listing, so an artifact that ever parsed
    # into an id range would corrupt it.
    assert [f.name for f in zone.files(DATASET)] == [landed.name]
    assert zone.max_landed_id(DATASET) == watermark == 100
    assert zone.files(ARTIFACT_DATASET) == []
    assert zone.max_landed_id(ARTIFACT_DATASET) == 0


def test_an_artifact_misfiled_into_the_dataset_prefix_still_cannot_move_the_watermark(zone):
    zone.write(DATASET, _landed_frame(1, 100), STAMP)

    zone.put_artifact(DATASET, model.artifact_name(STAMP), b"a model pickle")

    # Belt and braces on the prefix separation: _NAME_RE is the second guard, so
    # even a misconfigured artifact_dataset is inert rather than destructive.
    assert zone.max_landed_id(DATASET) == 100
    assert len(zone.files(DATASET)) == 1


def test_purging_a_dataset_leaves_the_artifacts_alone(zone):
    zone.write(DATASET, _landed_frame(1, 100), datetime(2026, 8, 1, 3, 0, 0))
    newest = zone.write(DATASET, _landed_frame(101, 200), datetime(2026, 8, 2, 3, 0, 0))
    uri = zone.put_artifact(ARTIFACT_DATASET, model.artifact_name(STAMP), b"a model pickle")

    removed = zone.purge_before(DATASET, datetime(2027, 1, 1))

    assert [f.name for f in zone.files(DATASET)] == [newest.name]
    assert len(removed) == 1
    # Retention is a property of the landed archive. A trained model going away
    # because the ETL's files aged out would break the registry rows pointing at it.
    assert zone.get_artifact(uri) == b"a model pickle"
    assert zone.max_landed_id(DATASET) == 200


# --- config coherence --------------------------------------------------------

def test_the_model_block_declares_every_key_fit_model_reads():
    block = _ml_config()["model"]

    # fit_model indexes these directly, so a missing one is a KeyError at
    # materialisation rather than a validation error at startup.
    assert MODEL_KEYS <= set(block)
    assert 0 < float(block["test_size"]) < 1


@pytest.mark.parametrize("env", ML_ENVS)
def test_every_ml_environment_declares_the_same_model_keys(env):
    # A key present in dev and missing in aws is a KeyError in the cluster, on
    # the one environment nobody runs by hand before deploying.
    assert set(_ml_config(env)["model"]) == set(_ml_config("dev")["model"]) == MODEL_KEYS


def test_the_rmse_threshold_sits_above_the_noise_floor():
    threshold = float(_ml_config()["asset_checks"]["max_holdout_rmse"])
    source = _yaml(ETL_CONFIG_DIR / "config.dev.yaml")["source"]
    noise_sigma = float(source["noise_sigma"])

    # The real invariant is the relationship between the two numbers, not either
    # of them: sigma is the floor no correctly specified model can beat, so a
    # threshold at or below it fails every run however good the model is, and
    # the check stops being about the model at all.
    assert threshold > noise_sigma

    # And the margin is wide enough for the data the generator actually makes.
    result = _train(_generated(int(source["num_rows"]), noise_sigma=noise_sigma))
    assert result.holdout_rmse < threshold
