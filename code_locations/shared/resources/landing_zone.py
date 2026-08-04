# shared/resources/landing_zone.py
"""
An immutable Parquet landing zone sitting between the operational database and
the warehouse.

Each extract writes exactly one file, named for the id range it covers and the
moment it was written:

    etl_table/etl_table__0000000101-0000000200__20260801T164258Z.parquet

That name IS the index. The landing zone needs no catalog and no sidecar state
store: the highest id already landed is one listing away, and the warehouse
loader works out which files it hasn't consumed by comparing their id range
against what it already holds. Ids are zero-padded so a lexicographic listing
is also a chronological one.

Files are never rewritten. That is the point: the warehouse can be wiped and
rebuilt by replaying the landing zone in order, without going back to Postgres
and without the operational database still having to hold every row it ever
wrote. Parquet rather than pickle because it is self-describing and readable by
anything -- including ClickHouse's own s3() table function, if a rebuild ever
wants to skip Python entirely.

ARTIFACTS ARE A SECOND, SMALLER JOB.
put_artifact/get_artifact store an opaque blob under a dataset prefix, and they
exist because the ML pipeline needs somewhere to put a trained model that is
`fs` locally and `s3` on EKS -- which is exactly the choice this module already
makes from config. Inventing a second storage abstraction to say the same thing
would mean two places to configure, two to get wrong in the cluster, and two
sets of credentials to arrange.

They are deliberately NOT part of the Parquet dataset protocol: an artifact has
no id range, so it is invisible to files(), max_landed_id() and purge_before().
That separation is load-bearing rather than tidy -- the extract's bookmark is
max(end_id) over the listing, and a model pickle appearing in that listing would
either be skipped by _NAME_RE (harmless) or, if it ever parsed, corrupt the
watermark. Artifacts live under their own dataset prefix so the two never mix.
"""
from __future__ import annotations

import io
import os
import re
from collections import namedtuple
from datetime import datetime, timezone

import pandas as pd

# etl_table__0000000101-0000000200__20260801T164258Z.parquet
_NAME_RE = re.compile(
    r"^(?P<dataset>.+)__(?P<start>\d{10})-(?P<end>\d{10})__(?P<landed_at>\d{8}T\d{6}Z)\.parquet$"
)
_ID_WIDTH = 10
_STAMP_FMT = "%Y%m%dT%H%M%SZ"

# uri is what gets stamped onto every row in the warehouse, so a row can always
# be traced back to the exact file it arrived in.
LandedFile = namedtuple("LandedFile", "uri name dataset start_id end_id landed_at")


def _parse(name: str, uri: str):
    """Turn a landing file name back into its metadata, or None if it isn't one."""
    match = _NAME_RE.match(name)
    if not match:
        return None

    try:
        landed_at = datetime.strptime(match.group("landed_at"), _STAMP_FMT)
    except ValueError:
        # The regex only asserts the stamp is digit-SHAPED; strptime is what
        # knows 20260229 is not a date in a non-leap year, and that T240000Z --
        # legal ISO 8601 for midnight -- is not one either.
        #
        # Returning rather than raising is the load-bearing part. files() feeds
        # max_landed_id, which is the extract's only bookmark, so an exception
        # escaping a directory listing does not fail one file: it makes the
        # whole dataset unlistable, and the extract, the loader and the
        # retention asset all fail on every run until a human finds and deletes
        # the offending name. A file that looks like a landed batch but cannot
        # be one is simply not a landed batch, which is what the docstring
        # above has always promised.
        return None

    return LandedFile(
        uri=uri,
        name=name,
        dataset=match.group("dataset"),
        start_id=int(match.group("start")),
        end_id=int(match.group("end")),
        landed_at=landed_at,
    )


def _build_name(dataset: str, start_id: int, end_id: int, landed_at: datetime) -> str:
    return (
        f"{dataset}__{start_id:0{_ID_WIDTH}d}-{end_id:0{_ID_WIDTH}d}"
        f"__{landed_at.strftime(_STAMP_FMT)}.parquet"
    )


# A whole dataset addressed as one table function, for replay. `uri_expr` and
# `landed_at_expr` are SQL that rebuild the same provenance the per-file load
# stamps as literals -- without them a replayed row would carry a different
# _source_file than the row it replaces, and provenance that changes depending
# on how a row arrived is worse than none.
GlobSource = namedtuple("GlobSource", "source uri_expr landed_at_expr")


def _landed_at_expr() -> str:
    r"""
    SQL recovering landed_at from the file name, for the glob path.

    The per-file loader passes landed.landed_at as a literal because it has the
    parsed object. A glob has only `_file`, so the timestamp comes back out of
    the name -- which is the point of encoding it there.

    One extract and one regex rewrite rather than six substring() calls:
    20260802T063339Z -> 2026-08-02 06:33:39, which parseDateTimeBestEffort takes
    unambiguously. Backslashes are doubled because ClickHouse string literals
    process escapes before the regex engine sees them.
    """
    stamp = r"extract(_file, '__(\\d{8}T\\d{6})Z\\.parquet$')"
    rewrite = r"'(\\d{4})(\\d{2})(\\d{2})T(\\d{2})(\\d{2})(\\d{2})'"
    return (
        f"parseDateTimeBestEffortOrNull(replaceRegexpOne({stamp}, {rewrite}, "
        r"'\\1-\\2-\\3 \\4:\\5:\\6'))"
    )


def _sql_string(value: str) -> str:
    """
    Quote a value for interpolation into a ClickHouse table-function call.

    These paths are built by _build_name and re-validated by _NAME_RE on the way
    back in, so they cannot contain a quote today. Escaping anyway because the
    string is going into SQL, and "it can't happen" is how it eventually does --
    a dataset name from config reaches this too.
    """
    escaped = value.replace("\\", "\\\\").replace("'", "\\'")
    return f"'{escaped}'"


class _LandingZone:
    """Shared behaviour; subclasses implement the four storage primitives."""

    def write(self, dataset: str, df: pd.DataFrame, landed_at: datetime = None) -> LandedFile:
        """Land a batch of rows as one immutable Parquet file, named for its id range."""
        if df.empty:
            raise ValueError(f"refusing to land an empty file for {dataset}")

        # Truncated to whole seconds, because that is the resolution the file
        # name records and the file name is the authority. Keeping microseconds
        # in memory would mean the live load stamps _landed_at = 07:03:26.376
        # while a replay of the same file recovers 07:03:26.000 from its name --
        # the same row carrying a different value depending on how it arrived,
        # which is worse than having no provenance at all. Dropping the
        # precision here makes the two agree by construction rather than by
        # anyone remembering to.
        landed_at = (landed_at or datetime.now(timezone.utc).replace(tzinfo=None)).replace(microsecond=0)
        start_id, end_id = int(df["id"].min()), int(df["id"].max())
        name = _build_name(dataset, start_id, end_id, landed_at)

        buffer = io.BytesIO()
        df.to_parquet(buffer, index=False)
        uri = self._put(dataset, name, buffer.getvalue())

        return LandedFile(uri, name, dataset, start_id, end_id, landed_at)

    @staticmethod
    def landed_file(payload) -> LandedFile:
        """
        Rebuild a LandedFile from the dict an asset passed downstream.

        The extract returns `landed._asdict()` rather than the namedtuple, so
        what crosses the io manager is a plain mapping. That costs one call here
        and means a pickle written by one version of the code cannot arrive at a
        differently-shaped namedtuple in the next.
        """
        return payload if isinstance(payload, LandedFile) else LandedFile(**payload)

    def read(self, landed: LandedFile) -> pd.DataFrame:
        return pd.read_parquet(io.BytesIO(self._get(landed)))

    def put_artifact(self, dataset: str, name: str, payload: bytes) -> str:
        """
        Store an opaque blob and return its uri. For model pickles and anything
        else that is not a Parquet batch.

        The caller owns the name, because the caller owns whatever versioning
        the artifact needs -- there is no id range here to derive one from. The
        ML pipeline timestamps its models; something else might use a run id.
        """
        return self._put(dataset, name, payload)

    def get_artifact(self, uri: str) -> bytes:
        """Read back what put_artifact wrote, by the uri it returned."""
        return self._read_uri(uri)

    def files(self, dataset: str) -> list:
        """Every landed file for a dataset, oldest id range first."""
        found = [f for f in (_parse(n, u) for n, u in self._list(dataset)) if f]
        return sorted(found, key=lambda f: (f.start_id, f.end_id))

    def files_after(self, dataset: str, watermark: int) -> list:
        """
        Files that may hold rows above `watermark`.

        Compares against end_id rather than start_id so a file that was only
        partially consumed is picked up again; the caller filters rows by id, and
        the warehouse's ReplacingMergeTree collapses anything re-delivered.
        """
        return [f for f in self.files(dataset) if f.end_id > watermark]

    def max_landed_id(self, dataset: str) -> int:
        """Highest id already landed, or 0 if nothing has been. The extract bookmark."""
        found = self.files(dataset)
        return max((f.end_id for f in found), default=0)

    def purge_before(self, dataset: str, cutoff: datetime) -> list:
        """
        Delete landed files older than `cutoff`. Returns what it removed.

        The archive is immutable, and retention is not a contradiction of that:
        a file is never rewritten, only eventually dropped whole. What replay
        loses is reach, not trust -- rebuild_warehouse_from_landing can go back
        as far as the retention horizon and no further.

        Two reasons this exists. The archive holds the raw change log, so a
        retracted post's text sits in it indefinitely, and purge_deleted_records
        cannot reach it -- it only knows about warehouse tables. And at firehose
        rate the zone grows by gigabytes a day, which is a disk problem on its
        own.

        THE NEWEST FILE IS NEVER DELETED, and that guard is load-bearing rather
        than defensive. The extract has no state store: its bookmark is
        max_landed_id, read straight off this listing. Empty the directory and
        that bookmark silently becomes 0, so the next run re-extracts the source
        table from the beginning and re-lands everything it already had. The
        warehouse would dedupe it, so nothing would look broken -- it would just
        quietly do all the work again. Keeping one file keeps the bookmark.

        Deletion is oldest-first by construction, so the surviving file is always
        the one holding the high-water mark.
        """
        found = self.files(dataset)
        if not found:
            return []

        # files() sorts by (start_id, end_id), so the last entry holds the
        # watermark. Compare on landed_at, spare that file whatever its age.
        newest = found[-1]
        doomed = [f for f in found if f.landed_at < cutoff and f.name != newest.name]

        for landed in doomed:
            self._delete(landed)
        return doomed

    def warehouse_source(self, landed: LandedFile) -> str | None:
        """
        A ClickHouse table-function expression for this file, or None if the
        warehouse cannot reach the storage this zone writes to.

        This is the whole interface between the two hierarchies: the zone knows
        where the bytes are, the loader knows what to do with them, and neither
        knows the other's type. A zone that returns a string gets the load pushed
        into the warehouse -- ClickHouse opens the Parquet itself and the bytes
        never enter this process. A zone that returns None falls back to reading
        the frame here.

        Returning None is the honest default: a new zone is not warehouse-readable
        until someone has arranged for it to be.
        """
        return None

    def warehouse_glob(self, dataset: str):
        """
        The whole dataset as one table function, or None.

        Only the replay uses this. The steady-state load is handed exactly the
        file the extract wrote, so it never needs to address a set -- which is
        the difference that keeps listing machinery out of a path that runs
        every 60 seconds. A rebuild is the opposite case: one statement over
        every file lets the warehouse open them in parallel instead of this
        process walking them one round trip at a time.
        """
        return None

    # --- storage primitives -------------------------------------------------
    def _put(self, dataset: str, name: str, payload: bytes) -> str:
        raise NotImplementedError

    def _get(self, landed: LandedFile) -> bytes:
        # A landed file is addressed by its uri like anything else here, so both
        # readers are one primitive. A zone implements _read_uri and gets the
        # Parquet path and the artifact path from it.
        return self._read_uri(landed.uri)

    def _read_uri(self, uri: str) -> bytes:
        raise NotImplementedError

    def _list(self, dataset: str) -> list:
        """[(file_name, uri), ...]"""
        raise NotImplementedError

    def _delete(self, landed: LandedFile) -> None:
        raise NotImplementedError


class LocalLandingZone(_LandingZone):
    """Landing zone on a local filesystem. Used by dev/uat/prod under compose."""

    def __init__(self, base_dir: str, warehouse_prefix: str = None):
        self.base_dir = base_dir
        # Where ClickHouse sees this directory, relative to its user_files_path.
        # The same Docker volume is mounted into both containers -- at
        # /app/landing here and /var/lib/clickhouse/user_files/<prefix> there --
        # so the warehouse can read a landed file without it passing through
        # this process. None means it isn't mounted, and the load falls back to
        # reading frames.
        self.warehouse_prefix = warehouse_prefix

    def _dataset_dir(self, dataset: str) -> str:
        return os.path.join(self.base_dir, dataset)

    def warehouse_source(self, landed: LandedFile) -> str | None:
        if not self.warehouse_prefix:
            return None
        # file() resolves relative to user_files_path and refuses to escape it,
        # so this is a path fragment rather than landed.uri (which is this
        # container's absolute path and means nothing to ClickHouse).
        path = f"{self.warehouse_prefix}/{landed.dataset}/{landed.name}"
        return f"file({_sql_string(path)}, 'Parquet')"

    def warehouse_glob(self, dataset: str):
        if not self.warehouse_prefix:
            return None
        pattern = f"{self.warehouse_prefix}/{dataset}/*.parquet"
        # _source_file is rebuilt from base_dir rather than taken from _path,
        # because ClickHouse sees this directory at its own mount point and the
        # per-file loader stamps the etl container's path. Same row, same string,
        # whichever way it was loaded.
        uri = f"concat({_sql_string(os.path.join(self.base_dir, dataset) + '/')}, _file)"
        return GlobSource(
            source=f"file({_sql_string(pattern)}, 'Parquet')",
            uri_expr=uri,
            landed_at_expr=_landed_at_expr(),
        )

    def _put(self, dataset: str, name: str, payload: bytes) -> str:
        directory = self._dataset_dir(dataset)
        os.makedirs(directory, exist_ok=True)
        path = os.path.join(directory, name)
        # Write to a temp name and rename, so a crash mid-write can't leave a
        # half-file that parses as a valid landed batch.
        tmp = f"{path}.partial"
        with open(tmp, "wb") as handle:
            handle.write(payload)
        os.replace(tmp, path)
        return path

    def _read_uri(self, uri: str) -> bytes:
        with open(uri, "rb") as handle:
            return handle.read()

    def _list(self, dataset: str) -> list:
        directory = self._dataset_dir(dataset)
        if not os.path.isdir(directory):
            return []
        return [(n, os.path.join(directory, n)) for n in os.listdir(directory)]

    def _delete(self, landed: LandedFile) -> None:
        # missing_ok: the retention asset and a human with rm are both allowed
        # to have removed it, and neither is an error worth failing a run over.
        try:
            os.remove(landed.uri)
        except FileNotFoundError:
            pass


class S3LandingZone(_LandingZone):
    """
    Landing zone on S3. Used by the aws environment.

    Reuses the platform bucket under its own prefix, alongside compute-logs/ and
    dagster-io/. Credentials come from the dagster service account via IRSA, so
    there is nothing to configure here beyond the bucket and prefix.
    """

    def __init__(self, bucket: str, prefix: str = "landing", region: str = None):
        self.bucket = bucket
        self.prefix = prefix.strip("/")
        # Only needed to build the endpoint URL ClickHouse reads from; boto3
        # picks its own region up from the environment. Falls back to AWS_REGION,
        # which the k8s manifests already set on every pod.
        self.region = region or os.environ.get("AWS_REGION")
        self._s3 = None

    @property
    def s3(self):
        # Built lazily so constructing the resource never needs credentials --
        # `dagster definitions validate` loads this in environments that have none.
        if self._s3 is None:
            import boto3

            self._s3 = boto3.client("s3")
        return self._s3

    def _key(self, dataset: str, name: str) -> str:
        return f"{self.prefix}/{dataset}/{name}"

    def _put(self, dataset: str, name: str, payload: bytes) -> str:
        key = self._key(dataset, name)
        self.s3.put_object(Bucket=self.bucket, Key=key, Body=payload)
        return f"s3://{self.bucket}/{key}"

    def _read_uri(self, uri: str) -> bytes:
        key = uri.split(f"s3://{self.bucket}/", 1)[1]
        return self.s3.get_object(Bucket=self.bucket, Key=key)["Body"].read()

    def warehouse_source(self, landed: LandedFile) -> str | None:
        if not self.region:
            return None
        # No credentials in the expression: ClickHouse authenticates to S3 with
        # the pod's own IRSA identity (use_environment_credentials in its
        # config.d), so this URL is all it needs. That is also why the region
        # matters -- the virtual-hosted endpoint has to be regional.
        key = self._key(landed.dataset, landed.name)
        url = f"https://{self.bucket}.s3.{self.region}.amazonaws.com/{key}"
        return f"s3({_sql_string(url)}, 'Parquet')"

    def warehouse_glob(self, dataset: str):
        if not self.region:
            return None
        prefix = f"https://{self.bucket}.s3.{self.region}.amazonaws.com/{self.prefix}/{dataset}/"
        # s3() fans the glob out across files and reads them in parallel, which
        # is the reason replay addresses the set rather than looping: one
        # statement, the warehouse's own concurrency, no round trip per object.
        return GlobSource(
            source=f"s3({_sql_string(prefix + '*.parquet')}, 'Parquet')",
            uri_expr=f"concat({_sql_string(f's3://{self.bucket}/{self.prefix}/{dataset}/')}, _file)",
            landed_at_expr=_landed_at_expr(),
        )

    def _delete(self, landed: LandedFile) -> None:
        key = landed.uri.split(f"s3://{self.bucket}/", 1)[1]
        # delete_object is idempotent -- deleting an absent key succeeds -- so
        # this needs no existence check.
        self.s3.delete_object(Bucket=self.bucket, Key=key)

    def _list(self, dataset: str) -> list:
        found = []
        paginator = self.s3.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=self.bucket, Prefix=f"{self.prefix}/{dataset}/"):
            for obj in page.get("Contents", []):
                name = obj["Key"].rsplit("/", 1)[-1]
                found.append((name, f"s3://{self.bucket}/{obj['Key']}"))
        return found


def build_landing_zone(cfg: dict):
    """Pick the landing zone implementation from config, like build_io_manager does."""
    zone_type = cfg["type"]

    if zone_type == "fs":
        return LocalLandingZone(
            base_dir=cfg["base_dir"],
            warehouse_prefix=cfg.get("warehouse_prefix"),
        )

    if zone_type == "s3":
        return S3LandingZone(
            bucket=cfg["bucket"],
            prefix=cfg["prefix"],
            region=cfg.get("region"),
        )

    raise ValueError(f"Unknown landing zone type '{zone_type}', expected 'fs' or 's3'")
