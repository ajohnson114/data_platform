"""
Replay the landing zone into the warehouse.

This is the other half of the archive's value, and the reason the landing zone
exists at all: wipe ClickHouse and this rebuilds it from the Parquet files,
without going back to the operational database and without it still having to
hold every row it ever wrote.

Deliberately a separate, manually triggered job rather than something the
steady-state load carries. The live path loads exactly the file the extract just
wrote -- one file, no listing, no watermark. Making it *also* able to notice and
catch up on arbitrary historical files would put the whole replay machinery in
the hot path, running every 60 seconds, to serve a case that comes up when
someone has just deliberately dropped a table.

An op rather than an asset: it produces nothing new, it re-produces something
that already has an owner. Modelling it as an asset would give the warehouse
tables two writers in the lineage graph.
"""
from dagster import Field, OpExecutionContext, job, op

from ..warehouse_targets import all_targets


@op(
    required_resource_keys={"landing_zone", "warehouse_loader"},
    config_schema={
        # Replay from a point rather than from the start, for the case where the
        # warehouse lost a recent window rather than everything. files_after
        # compares against end_id, so a partially consumed file is included
        # again and the sorting key collapses whatever gets re-delivered.
        "from_id": Field(int, default_value=0, description="Replay files holding ids above this."),
    },
)
def replay_landing_zone(context: OpExecutionContext) -> None:
    loader = context.resources.warehouse_loader
    from_id = context.op_config["from_id"]

    for target in all_targets():
        context.log.info(
            f"{target.dataset}: replaying into {target.table} "
            f"from id {from_id} via {type(loader).__name__}"
        )

        # One statement per dataset. Against the direct loader that hands the
        # whole prefix to ClickHouse, which fans out across the objects and
        # reads them concurrently -- the reason replay addresses a set rather
        # than looping like the steady-state load does. Re-running is safe:
        # anything already present collapses on the sorting key.
        result = loader.replay(
            target.table,
            target.dataset,
            order_by=target.order_by,
            version_column=target.version_column,
            is_deleted_column=target.is_deleted_column,
            partition_by=target.partition_by,
            from_id=from_id,
        )
        context.log.info(f"{target.table}: {result.rows_loaded} rows from {result.source}")


@job(description="Rebuild the warehouse tables by replaying the Parquet landing zone.")
def rebuild_warehouse_from_landing():
    replay_landing_zone()
