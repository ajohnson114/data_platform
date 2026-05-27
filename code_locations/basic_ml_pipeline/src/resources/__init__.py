from dagster import FilesystemIOManager
from shared.resources.db_client_resource import DBClientResource
from shared.resources import global_resources
from config.config import get_config

local_resources = {
    'ml_postgres': DBClientResource(**get_config().get_postgres_creds()),
    'fs_io_manager': FilesystemIOManager(base_dir=get_config().get_fs_io_manager_base_dir()),
}

# N.B. the if the resource keys from the local_resources match the global ones
# they will overwrite the global
all_resources = global_resources | local_resources