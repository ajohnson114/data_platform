# shared/resources/io_managers.py
from dagster import FilesystemIOManager
from dagster_aws.s3 import S3PickleIOManager, S3Resource


def build_io_manager(cfg: dict):
    """
    Build the io manager that carries intermediate values between steps.
    Stock Dagster io managers only: the filesystem locally, S3 in the cloud.
    """
    io_manager_type = cfg['type']

    if io_manager_type == 'fs':
        return FilesystemIOManager(base_dir=cfg['base_dir'])

    if io_manager_type == 's3':
        return S3PickleIOManager(
            s3_resource=S3Resource(),
            s3_bucket=cfg['bucket'],
            s3_prefix=cfg['prefix'],
        )

    raise ValueError(f"Unknown io manager type '{io_manager_type}', expected 'fs' or 's3'")
