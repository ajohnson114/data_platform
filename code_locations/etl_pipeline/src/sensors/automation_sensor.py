"""
The sensor that evaluates AutomationConditions.

Declared explicitly rather than left to Dagster, and that is the whole point of
this file. Dagster synthesises a `default_automation_condition_sensor` for any
code location whose assets carry automation conditions, which is convenient
right up until you notice it is created with `default_status=STOPPED`. The dbt
marts would then ship with a rebuild condition and nothing running to evaluate
it -- a feature that looks implemented on the graph and never fires, until
someone finds the toggle in the UI.

Naming it here makes it start with the code location, the same way
`bsky_record_sensor` does, and puts the reason in the repo instead of in
whoever remembers to flip it after `make reset`.

Note this is what evaluates the cron, not a ScheduleDefinition. The condition
needs somewhere to be evaluated, and an AutomationConditionSensorDefinition is
that place; the cron lives inside the condition rather than on a schedule so
that the in-progress and missing guards come with it.

Scoped to the dbt group rather than left global. Every other asset in this code
location is driven by the sensor, a schedule, or a person; this sensor exists
for exactly one thing, and a selection says so more clearly than a comment
would. It also means a future asset that grows an automation condition has to
opt in here, rather than silently joining a sensor that was already running.
"""
from dagster import AssetSelection, AutomationConditionSensorDefinition, DefaultSensorStatus

automation_condition_sensor = AutomationConditionSensorDefinition(
    name="dbt_rebuild_sensor",
    target=AssetSelection.groups("analytics_dbt"),
    default_status=DefaultSensorStatus.RUNNING,
    # Tags every run this sensor launches, so the instance can cap how many of
    # them execute at once (see tag_concurrency_limits in deployment/dagster.yaml
    # and deployment/k8s/10-dagster-instance.yaml). The condition in src/dbt.py
    # stops a backlog being *created*; this stops any backlog that does exist --
    # a manual re-materialise, a retry storm -- from being released all at once.
    # The two are separate defences and the 2026-08-03 recovery needed both.
    run_tags={"rebuild": "dbt"},
    # The condition is a cron tick, so this only bounds how late a rebuild can
    # start: 30 seconds of jitter against a 5 minute interval. Evaluating faster
    # would just re-check a cron that has not ticked.
    minimum_interval_seconds=30,
    description=(
        "Rebuilds the dbt marts on the schedule in "
        "data_pipeline.schedules.dbt_rebuild. Evaluates REBUILD_ON_CRON "
        "(src/dbt.py). The marts' freshness policies are alarms and are not "
        "read by this sensor."
    ),
)
