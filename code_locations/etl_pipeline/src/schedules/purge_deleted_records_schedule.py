from dagster import ScheduleDefinition, DefaultScheduleStatus
from config.config import get_config

# Referenced by name rather than by importing the job, the same way
# bsky_record_sensor does, so schedules don't import jobs and jobs don't have to
# know schedules exist.
purge_deleted_records_schedule = ScheduleDefinition(
    name="purge_deleted_records_schedule",
    job_name="purge_job",
    cron_schedule=get_config().get_purge_deleted_records_cron(),
    default_status=DefaultScheduleStatus.RUNNING,
)
