from dagster import define_asset_job

failing_job = define_asset_job(
    name="failing_job", selection=["pull_data_from_other_source","show_stack_trace_for_returning_wrong_type",
                                   "do_not_clean_data", 'do_other_operation']
)