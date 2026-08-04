"""
dbt, orchestrated by Dagster, inside the ETL code location.

Deliberately not a third code location. A separate deployment unit is how you
isolate a team's dependencies and blast radius, and four models owned by the
same people who own the loader do not need either -- it would buy a container, a
Dockerfile, a k8s manifest and a workspace entry in exchange for a busier
diagram.

Three things here are load-bearing:

  1. The dbt source maps onto the EXISTING asset key. Without that, dbt's
     lineage is an island: `bsky_records_snapshot` appears twice, once as a
     Dagster asset and once as a dbt source, and nothing shows that the marts go
     stale when the load fails. See _Translator.

  2. dbt's credentials come from this code location's config, never its own.
     profiles.yml is entirely env_var references and they are set here from
     get_clickhouse_creds(), so there is no path by which dbt connects somewhere
     the rest of the platform does not.

  3. The marts keep themselves in sync with the stream, via a freshness policy
     and an automation condition that reads it. See MART_FRESHNESS below.
"""
import os
from datetime import timedelta
from pathlib import Path

from dagster import (
    AssetExecutionContext,
    AssetKey,
    AutomationCondition,
    FreshnessPolicy,
    apply_freshness_policy,
)
from dagster_dbt import DagsterDbtTranslator, DbtCliResource, DbtProject, dbt_assets

from config.config import get_config

# Baked into the image next to src/ and config/. `dbt parse` runs at build time
# so the manifest exists before the code server starts -- building assets from a
# manifest generated at import time would make definition loading depend on the
# warehouse being reachable.
DBT_PROJECT_DIR = Path(__file__).parent.parent / "dbt"

dbt_project = DbtProject(
    project_dir=DBT_PROJECT_DIR,
    profiles_dir=DBT_PROJECT_DIR,
)


def dbt_env() -> dict:
    """
    The warehouse credentials, in the shape profiles.yml reads them.

    One source of truth: this is the same get_clickhouse_creds() the
    ClickHouseResource is built from, so dbt and the loader cannot drift onto
    different hosts or different databases.
    """
    creds = get_config().get_clickhouse_creds()
    return {
        "DBT_CLICKHOUSE_HOST": str(creds["host"]),
        "DBT_CLICKHOUSE_PORT": str(creds["port"]),
        "DBT_CLICKHOUSE_USER": str(creds["username"]),
        "DBT_CLICKHOUSE_PASSWORD": str(creds["password"]),
        "DBT_CLICKHOUSE_DATABASE": str(creds["database"]),
    }


def _minutes(spec: dict, key: str) -> timedelta:
    return timedelta(minutes=float(spec[key]))


_mart_windows = get_config().get_mart_freshness()
_REBUILD_CRON = get_config().get_dbt_rebuild_cron()

# How stale a mart has to be before anyone is told. An ALARM, not a trigger.
#
# A FreshnessPolicy runs nothing. The FreshnessDaemon evaluates it every 30
# seconds and writes PASS/WARN/FAIL per asset; that state shows in the UI and
# can be alerted on, and nothing else happens. What rebuilds the marts is
# REBUILD_ON_CRON below, which is deliberately independent of this.
#
# The two used to be one thing -- the condition read `freshness_warned()`, so
# the warn window doubled as the rebuild cadence. That was tidy and it conflated
# two questions: "how often should this run" is a cost decision, and "when
# should someone be told it hasn't" is an SLA. Separating them costs one config
# key and means the alarm can be moved without changing the bill.
#
# The windows therefore have to sit CLEAR OF the cadence. A mart on a 5 minute
# cron is routinely just under 5 minutes stale immediately before its next
# rebuild, so warning at 5 would mean warning always. 8 leaves the cron a missed
# tick of headroom; 15 is three missed ticks, by which point it is not a blip.
MART_FRESHNESS = FreshnessPolicy.time_window(
    fail_window=_minutes(_mart_windows, "fail_after_minutes"),
    warn_window=_minutes(_mart_windows, "warn_after_minutes"),
)

# ...and, separately, what rebuilds them.
#
# A cron, because the cost of a rebuild is set by how much history there is and
# not by how stale the marts are. fct_posts and dim_authors re-read the whole
# record stream, so the honest question is "how often can we afford this",
# which is a fixed interval, not a function of lag.
#
# Deliberately NOT AutomationCondition.eager(), which fires on any dependency
# update: the upstream snapshot lands every 60 seconds, so eager would rebuild
# three tables a minute to fold in a minute of a stream.
#
# `missing()` is what bootstraps a cold warehouse, and it is also the only
# branch that does anything for the staging view -- a view has no freshness to
# be behind on and needs building exactly once. Without it the marts would sit
# empty until the first cron tick, which is a worse first impression than it
# sounds when the tick is five minutes after `make`.
REBUILD_ON_CRON = (
    (
        AutomationCondition.missing()
        | (
            AutomationCondition.cron_tick_passed(_REBUILD_CRON)
            # AND the snapshot actually moved. The cron says how OFTEN a rebuild
            # may happen; this says whether there is anything to rebuild FOR.
            #
            # Added after 2026-08-03. `~in_progress()` below was supposed to stop
            # rebuilds stacking, and it does not, because a run that FAILED is
            # not in progress: while the warehouse was down every dbt run failed
            # in seconds, so every tick evaluated true and submitted another. The
            # queue absorbed them silently and released the backlog the moment
            # ClickHouse answered -- twelve concurrent `SELECT ... FINAL` over a
            # 6.2M-row table, which is how a recovery turned into a second
            # outage.
            #
            # Gating on the dependency closes it at the source. If ingest is
            # broken the snapshot is not updating either, so no rebuild is
            # *created* rather than sixteen being created and queued. In steady
            # state the snapshot lands every 60 seconds and the cron is 5
            # minutes, so every tick still has new data and the cadence is
            # unchanged -- this only bites when the stream is stopped or the
            # platform is unwell, which is exactly when it should.
            & AutomationCondition.any_deps_updated()
        )
    )
    # Don't build a mart on top of a snapshot that isn't there yet, and don't
    # queue a rebuild behind one still running. The last guard is what a bare
    # ScheduleDefinition would not give us: a cron schedule fires whether or not
    # the last run finished, so as the rebuild grows past the interval it would
    # start stacking. Here it simply skips the tick. It is necessary and, per the
    # incident above, not sufficient.
    & ~AutomationCondition.any_deps_missing()
    & ~AutomationCondition.any_deps_in_progress()
    & ~AutomationCondition.in_progress()
).with_label(f"rebuild_on_cron({_REBUILD_CRON})_when_deps_updated")


class _Translator(DagsterDbtTranslator):
    """Maps dbt nodes onto Dagster asset keys, policies and automation."""

    @staticmethod
    def _tags(props) -> list:
        return list(props.get("tags") or [])

    def get_asset_spec(self, manifest, unique_id, project):
        """Attach the freshness policy to the marts, and only to the marts.

        Keyed off the dbt tag the models already carry (`tags = ['marts']` in
        each model's config block) rather than a list of names here, so a new
        mart inherits the policy by being a mart.

        `stg_bsky_records` deliberately gets none. It is materialised as a view,
        so it is re-read from the snapshot on every query and cannot be stale --
        giving it a freshness policy would put a clock on something that has no
        staleness to measure, and it would go red during any quiet period purely
        because nothing had re-run a view that did not need re-running.
        """
        spec = super().get_asset_spec(manifest, unique_id, project)
        props = (
            manifest.get("nodes", {}).get(unique_id)
            or manifest.get("sources", {}).get(unique_id)
            or {}
        )
        if "marts" in self._tags(props):
            spec = apply_freshness_policy(spec, MART_FRESHNESS)
        return spec

    def get_automation_condition(self, dbt_resource_props):
        # Every model, not just the marts. The staging view is a view: rebuilding
        # it on the cron is close to free (it is a CREATE OR REPLACE VIEW, not a
        # scan) and the missing() branch is what creates it in the first place.
        return REBUILD_ON_CRON

    def get_asset_key(self, props) -> AssetKey:
        # A dbt source must resolve to the asset key the loader already
        # materialises, or the graph shows two unconnected halves. dbt names the
        # source `warehouse.bsky_records_snapshot`; Dagster knows it as
        # `bsky_records_snapshot`. Collapsing to the table name is what joins
        # them, and it is why the marts turn stale in the UI when the load fails.
        if props.get("resource_type") == "source":
            return AssetKey(props["name"])
        return super().get_asset_key(props)

    def get_group_name(self, props):
        # Own group, so the marts read as a distinct layer in the lineage rather
        # than being mixed into the ingest assets they depend on.
        return "analytics_dbt"


@dbt_assets(
    manifest=dbt_project.manifest_path,
    dagster_dbt_translator=_Translator(),
)
def dbt_analytics_assets(context: AssetExecutionContext, dbt: DbtCliResource):
    """Build the analytics models.

    `dbt build` rather than `dbt run`: build interleaves tests with models and
    stops a failing model's children from being built on top of it. `dbt run`
    followed by `dbt test` would materialise everything first and only then
    report that a grain was wrong -- by which point the bad table is already
    what the NL-to-SQL service is querying.
    """
    yield from dbt.cli(["build"], context=context).stream()


def build_dbt_resource() -> DbtCliResource:
    # Credentials are injected into the process env rather than written to disk,
    # so nothing lands a rendered profiles.yml containing a password next to the
    # project.
    os.environ.update(dbt_env())
    return DbtCliResource(project_dir=dbt_project, profiles_dir=str(DBT_PROJECT_DIR))
