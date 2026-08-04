"""
Unit tests for the immutable Parquet landing zone.

The module's central claim is that the file name IS the index: the id range and
the moment a batch landed both come back out of the name, with no catalog and no
sidecar state. Most of what follows is that round trip, plus the two places it
is load-bearing -- max_landed_id, which is the extract's only bookmark, and
purge_before, which must not destroy it.

LocalLandingZone is tested against real temp directories rather than a fake,
because the atomic rename and the directory listing are the behaviour under
test. S3LandingZone is driven through a stub client: boto3 is not installed, and
its interesting logic is key and URI string handling anyway.
"""
import io
import os
import re
from datetime import datetime

import pandas as pd
import pytest

from conftest import load_module

landing_zone = load_module("code_locations/shared/resources/landing_zone.py")

LandedFile = landing_zone.LandedFile
DATASET = "etl_table"
STAMP = datetime(2026, 8, 1, 16, 42, 58)


def _frame(start_id: int, end_id: int) -> pd.DataFrame:
    ids = list(range(start_id, end_id + 1))
    return pd.DataFrame({"id": ids, "text": [f"row-{i}" for i in ids]})


@pytest.fixture
def zone(tmp_path):
    return landing_zone.LocalLandingZone(base_dir=str(tmp_path))


class _FakeS3:
    """Enough of the boto3 S3 client to exercise the four storage primitives."""

    def __init__(self):
        self.objects = {}

    def put_object(self, Bucket, Key, Body):
        self.objects[(Bucket, Key)] = Body

    def get_object(self, Bucket, Key):
        return {"Body": io.BytesIO(self.objects[(Bucket, Key)])}

    def delete_object(self, Bucket, Key):
        self.objects.pop((Bucket, Key), None)

    def get_paginator(self, operation):
        assert operation == "list_objects_v2"
        return self

    def paginate(self, Bucket, Prefix):
        # One page is enough; the caller's loop over pages is the same either way.
        keys = [k for (b, k) in sorted(self.objects) if b == Bucket and k.startswith(Prefix)]
        return [{"Contents": [{"Key": k} for k in keys]}]


def _s3_zone(bucket="platform", prefix="landing", region="us-east-1"):
    zone = landing_zone.S3LandingZone(bucket=bucket, prefix=prefix, region=region)
    zone._s3 = _FakeS3()
    return zone


# --- the name as the index ---------------------------------------------------

def test_build_name_and_parse_round_trip():
    name = landing_zone._build_name(DATASET, 101, 200, STAMP)
    parsed = landing_zone._parse(name, "/landing/etl_table/" + name)

    assert parsed.dataset == DATASET
    assert (parsed.start_id, parsed.end_id) == (101, 200)
    assert parsed.landed_at == STAMP
    assert parsed.name == name
    assert parsed.uri == "/landing/etl_table/" + name


def test_parse_recovers_a_dataset_name_containing_the_separator():
    # The dataset group is a greedy `.+` over a `__`-separated name, so a dataset
    # that itself contains `__` is the case that could go wrong. It does not: the
    # tail of the pattern is rigid and anchored, so backtracking has exactly one
    # place to split.
    name = landing_zone._build_name("etl__table", 1, 2, STAMP)
    parsed = landing_zone._parse(name, name)

    assert parsed.dataset == "etl__table"
    assert (parsed.start_id, parsed.end_id) == (1, 2)


def test_zero_padding_makes_lexicographic_order_id_order():
    ids = [5, 50, 5000]
    names = [landing_zone._build_name(DATASET, i, i, STAMP) for i in ids]

    assert sorted(names) == names
    assert [landing_zone._parse(n, n).start_id for n in sorted(names)] == ids


@pytest.mark.parametrize("name", [
    "etl_table__0000000101-0000000200__20260801T164258Z.parquet.partial",
    "etl_table__0000000101-0000000200.parquet",
    "etl_table__0000000101-0000000200__20260801T164258Z.csv",
    "etl_table__101-200__20260801T164258Z.parquet",
    "etl_table__0000000101-0000000200__20260801T164258Z.parquet.gz",
    "_SUCCESS",
    "notes.txt",
    "",
])
def test_parse_returns_none_for_anything_that_is_not_a_landed_file(name):
    assert landing_zone._parse(name, f"/landing/etl_table/{name}") is None


def test_parse_returns_none_for_a_shaped_name_with_an_impossible_timestamp():
    # 2026 is not a leap year, and hour 24 is legal ISO 8601 for midnight. Both
    # satisfy \d{8}T\d{6}Z, so the regex admits them and strptime rejects them.
    # Regression: strptime's ValueError used to propagate out of files(), and
    # since files() feeds max_landed_id -- the extract's only bookmark -- one
    # such name made the entire dataset unlistable rather than being ignored
    # like any other stray file.
    for stamp in ("20260229T000000Z", "20260801T240000Z"):
        name = f"etl_table__0000000101-0000000200__{stamp}.parquet"
        assert landing_zone._parse(name, name) is None


def test_an_impossible_timestamp_does_not_break_the_watermark(zone, tmp_path):
    """The blast radius the fix is really about: one bad name must not take the
    dataset's bookmark down with it."""
    zone.write(DATASET, pd.DataFrame({"id": [1, 2, 3]}))
    bad = tmp_path / DATASET / f"{DATASET}__0000000009-0000000009__20260229T000000Z.parquet"
    bad.write_bytes(b"not parquet either")

    # The good batch is still found, the unparseable name is simply skipped.
    assert zone.max_landed_id(DATASET) == 3
    assert [f.end_id for f in zone.files(DATASET)] == [3]


# --- write -------------------------------------------------------------------

def test_write_refuses_an_empty_frame(zone):
    with pytest.raises(ValueError, match=DATASET):
        zone.write(DATASET, pd.DataFrame({"id": []}))


def test_write_truncates_landed_at_to_whole_seconds(zone):
    landed = zone.write(DATASET, _frame(1, 3), datetime(2026, 8, 1, 16, 42, 58, 376000))

    assert landed.landed_at.microsecond == 0
    # The property the truncation exists for: what the live load stamps and what
    # a replay recovers from the name are the same value, not merely close.
    assert landing_zone._parse(landed.name, landed.uri).landed_at == landed.landed_at


def test_write_derives_the_id_range_from_the_frame(zone):
    df = _frame(101, 200).sample(frac=1, random_state=0)  # order must not matter
    landed = zone.write(DATASET, df, STAMP)

    assert (landed.start_id, landed.end_id) == (101, 200)
    assert landed.name == landing_zone._build_name(DATASET, 101, 200, STAMP)


def test_write_read_round_trips_the_rows_through_real_parquet(zone):
    df = _frame(101, 110)
    landed = zone.write(DATASET, df, STAMP)

    assert os.path.isfile(landed.uri)
    pd.testing.assert_frame_equal(zone.read(landed), df)


def test_write_leaves_no_partial_file_behind(zone, tmp_path):
    landed = zone.write(DATASET, _frame(1, 5), STAMP)

    listing = os.listdir(tmp_path / DATASET)
    assert listing == [landed.name]
    assert not any(n.endswith(".partial") for n in listing)


# --- listing -----------------------------------------------------------------

def test_files_returns_oldest_id_range_first(zone):
    for start, end in [(21, 30), (1, 10), (11, 20)]:
        zone.write(DATASET, _frame(start, end), STAMP)

    assert [(f.start_id, f.end_id) for f in zone.files(DATASET)] == [(1, 10), (11, 20), (21, 30)]


def test_files_ignores_files_that_are_not_landed_batches(zone, tmp_path):
    landed = zone.write(DATASET, _frame(1, 10), STAMP)
    directory = tmp_path / DATASET
    (directory / "README.txt").write_text("hands off")
    (directory / f"{landed.name}.partial").write_bytes(b"half a file")
    (directory / "etl_table__1-10__20260801T164258Z.parquet").write_bytes(b"unpadded")

    assert [f.name for f in zone.files(DATASET)] == [landed.name]


def test_files_returns_empty_for_a_dataset_that_has_never_landed(zone):
    assert zone.files("never_seen") == []


# --- the bookmark ------------------------------------------------------------

def test_max_landed_id_is_zero_until_something_lands(zone):
    assert zone.max_landed_id("never_seen") == 0


def test_max_landed_id_is_the_highest_end_id(zone):
    zone.write(DATASET, _frame(1, 10), STAMP)
    zone.write(DATASET, _frame(11, 200), STAMP)

    assert zone.max_landed_id(DATASET) == 200


def test_files_after_returns_a_file_straddling_the_watermark(zone):
    zone.write(DATASET, _frame(1, 100), STAMP)
    straddling = zone.write(DATASET, _frame(101, 200), STAMP)

    # 150 is inside the second file: it was only partially consumed, so it has to
    # come back, and the caller filters rows by id.
    assert [f.name for f in zone.files_after(DATASET, 150)] == [straddling.name]


def test_files_after_excludes_a_file_fully_consumed(zone):
    zone.write(DATASET, _frame(1, 100), STAMP)

    assert zone.files_after(DATASET, 100) == []


# --- retention ---------------------------------------------------------------

def _land_days(zone, batches):
    """Land (start, end, day-of-August) batches and return them in write order."""
    return [
        zone.write(DATASET, _frame(start, end), datetime(2026, 8, day, 3, 0, 0))
        for start, end, day in batches
    ]


def test_purge_before_removes_and_returns_files_older_than_the_cutoff(zone):
    landed = _land_days(zone, [(1, 10, 1), (11, 20, 2), (21, 30, 3), (31, 40, 4)])

    removed = zone.purge_before(DATASET, datetime(2026, 8, 3, 0, 0, 0))

    assert [f.name for f in removed] == [landed[0].name, landed[1].name]
    assert [f.name for f in zone.files(DATASET)] == [landed[2].name, landed[3].name]
    assert not any(os.path.exists(f.uri) for f in removed)


def test_purge_before_never_removes_the_newest_file(zone):
    landed = _land_days(zone, [(1, 10, 1), (11, 20, 2), (21, 30, 3)])

    # Cutoff past every file: the guard is the only thing keeping one alive.
    removed = zone.purge_before(DATASET, datetime(2027, 1, 1))

    assert [f.name for f in removed] == [landed[0].name, landed[1].name]
    assert [f.name for f in zone.files(DATASET)] == [landed[2].name]


def test_max_landed_id_survives_a_purge_of_everything(zone):
    _land_days(zone, [(1, 10, 1), (11, 20, 2), (21, 30, 3)])
    before = zone.max_landed_id(DATASET)

    zone.purge_before(DATASET, datetime(2027, 1, 1))

    # The whole reason the newest file is spared: the bookmark lives in the
    # listing, so an emptied directory would silently re-extract from id 0.
    assert zone.max_landed_id(DATASET) == before == 30


def test_purge_before_keeps_a_sole_file_and_returns_nothing(zone):
    landed = _land_days(zone, [(1, 10, 1)])

    assert zone.purge_before(DATASET, datetime(2027, 1, 1)) == []
    assert [f.name for f in zone.files(DATASET)] == [landed[0].name]


def test_purge_before_returns_empty_for_a_dataset_that_does_not_exist(zone):
    assert zone.purge_before("never_seen", datetime(2027, 1, 1)) == []


def test_purge_before_leaves_files_newer_than_the_cutoff(zone):
    landed = _land_days(zone, [(1, 10, 5), (11, 20, 6)])

    assert zone.purge_before(DATASET, datetime(2026, 8, 1)) == []
    assert [f.name for f in zone.files(DATASET)] == [f.name for f in landed]


# --- warehouse handoff -------------------------------------------------------

def test_local_warehouse_source_is_none_when_the_zone_is_not_mounted(zone):
    landed = zone.write(DATASET, _frame(1, 10), STAMP)

    assert zone.warehouse_source(landed) is None
    assert zone.warehouse_glob(DATASET) is None


def test_local_warehouse_source_addresses_the_file_relative_to_the_prefix(tmp_path):
    zone = landing_zone.LocalLandingZone(str(tmp_path), warehouse_prefix="landing")
    landed = zone.write(DATASET, _frame(1, 10), STAMP)

    # A path fragment under ClickHouse's user_files_path, not this container's uri.
    assert zone.warehouse_source(landed) == f"file('landing/{DATASET}/{landed.name}', 'Parquet')"


def test_local_warehouse_glob_rebuilds_the_uri_the_per_file_load_stamps(tmp_path):
    zone = landing_zone.LocalLandingZone(str(tmp_path), warehouse_prefix="landing")
    landed = zone.write(DATASET, _frame(1, 10), STAMP)
    glob = zone.warehouse_glob(DATASET)

    assert glob.source == f"file('landing/{DATASET}/*.parquet', 'Parquet')"
    # concat(<literal>, _file) has to produce exactly landed.uri, or a replayed
    # row would carry a different _source_file than the row it replaces.
    assert glob.uri_expr == f"concat('{os.path.dirname(landed.uri)}/', _file)"


def test_s3_warehouse_source_is_none_without_a_region(monkeypatch):
    monkeypatch.delenv("AWS_REGION", raising=False)
    zone = landing_zone.S3LandingZone(bucket="platform", prefix="landing")
    landed = LandedFile("s3://platform/landing/etl_table/x.parquet", "x.parquet",
                        DATASET, 1, 10, STAMP)

    assert zone.region is None
    assert zone.warehouse_source(landed) is None
    assert zone.warehouse_glob(DATASET) is None


def test_s3_warehouse_source_is_a_regional_virtual_hosted_url():
    zone = _s3_zone(region="eu-west-2")
    landed = LandedFile("s3://platform/landing/etl_table/x.parquet", "x.parquet",
                        DATASET, 1, 10, STAMP)

    assert zone.warehouse_source(landed) == (
        "s3('https://platform.s3.eu-west-2.amazonaws.com/landing/etl_table/x.parquet', 'Parquet')"
    )
    assert zone.warehouse_glob(DATASET).source == (
        "s3('https://platform.s3.eu-west-2.amazonaws.com/landing/etl_table/*.parquet', 'Parquet')"
    )


def test_landed_at_expr_recovers_the_timestamp_write_recorded():
    # The expression is ClickHouse SQL, so run its two regexes here the way
    # ClickHouse would: the literal's doubled backslashes are halved before the
    # regex engine sees them.
    extract_pat, rewrite_pat, replacement = (
        literal.replace("\\\\", "\\")
        for literal in re.findall(r"'([^']*)'", landing_zone._landed_at_expr())
    )
    name = landing_zone._build_name(DATASET, 1, 10, STAMP)

    stamp = re.search(extract_pat, name).group(1)
    assert re.sub(rewrite_pat, replacement, stamp) == "2026-08-01 16:42:58"


def test_sql_string_escapes_quotes_and_backslashes():
    assert landing_zone._sql_string("plain") == "'plain'"
    assert landing_zone._sql_string("o'brien") == r"'o\'brien'"
    assert landing_zone._sql_string("a\\b") == r"'a\\b'"
    # Order matters: escaping quotes first would leave the added backslash to be
    # doubled by the backslash pass, breaking the escape it just made.
    assert landing_zone._sql_string("a\\'b") == r"'a\\\'b'"


# --- s3 storage primitives ---------------------------------------------------

def test_s3_write_stores_under_the_prefixed_key_and_returns_an_s3_uri():
    zone = _s3_zone(prefix="/landing/")  # stray slashes must not double up
    landed = zone.write(DATASET, _frame(1, 10), STAMP)

    key = f"landing/{DATASET}/{landed.name}"
    assert landed.uri == f"s3://platform/{key}"
    assert ("platform", key) in zone._s3.objects


def test_s3_read_and_list_parse_the_key_back_out_of_the_uri():
    zone = _s3_zone()
    df = _frame(1, 10)
    landed = zone.write(DATASET, df, STAMP)

    assert [f.name for f in zone.files(DATASET)] == [landed.name]
    pd.testing.assert_frame_equal(zone.read(zone.files(DATASET)[0]), df)


def test_s3_purge_deletes_the_object_behind_the_uri():
    zone = _s3_zone()
    old = zone.write(DATASET, _frame(1, 10), datetime(2026, 8, 1))
    newest = zone.write(DATASET, _frame(11, 20), datetime(2026, 8, 2))

    removed = zone.purge_before(DATASET, datetime(2027, 1, 1))

    assert [f.name for f in removed] == [old.name]
    assert list(zone._s3.objects) == [("platform", f"landing/{DATASET}/{newest.name}")]
    assert zone.max_landed_id(DATASET) == 20


# --- construction ------------------------------------------------------------

def test_build_landing_zone_builds_a_local_zone_from_fs_config(tmp_path):
    zone = landing_zone.build_landing_zone(
        {"type": "fs", "base_dir": str(tmp_path), "warehouse_prefix": "landing"}
    )

    assert isinstance(zone, landing_zone.LocalLandingZone)
    assert (zone.base_dir, zone.warehouse_prefix) == (str(tmp_path), "landing")


def test_build_landing_zone_defaults_the_warehouse_prefix_to_unmounted(tmp_path):
    zone = landing_zone.build_landing_zone({"type": "fs", "base_dir": str(tmp_path)})

    assert zone.warehouse_prefix is None


def test_build_landing_zone_builds_an_s3_zone_from_s3_config():
    zone = landing_zone.build_landing_zone(
        {"type": "s3", "bucket": "platform", "prefix": "landing", "region": "eu-west-1"}
    )

    assert isinstance(zone, landing_zone.S3LandingZone)
    assert (zone.bucket, zone.prefix, zone.region) == ("platform", "landing", "eu-west-1")


def test_build_landing_zone_rejects_an_unknown_type():
    with pytest.raises(ValueError) as excinfo:
        landing_zone.build_landing_zone({"type": "gcs", "bucket": "platform"})

    message = str(excinfo.value)
    assert "gcs" in message and "fs" in message and "s3" in message


def test_landed_file_accepts_the_dict_that_crossed_the_io_manager():
    landed = LandedFile("/landing/etl_table/x.parquet", "x.parquet", DATASET, 1, 10, STAMP)

    assert landing_zone._LandingZone.landed_file(landed._asdict()) == landed


def test_landed_file_passes_an_existing_landed_file_through():
    landed = LandedFile("/landing/etl_table/x.parquet", "x.parquet", DATASET, 1, 10, STAMP)

    assert landing_zone._LandingZone.landed_file(landed) is landed
