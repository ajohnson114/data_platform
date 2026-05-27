from .etl_job import etl_job
from .failing_job import failing_job
from .streaming_ingest_job import streaming_ingest_job

all_jobs = [etl_job, failing_job, streaming_ingest_job]