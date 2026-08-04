from shared.resources.db_client_resource import DBClientResource
from shared.resources import build_global_resources
from config.config import get_config
from ..dbt import build_dbt_resource

# I want each code location to have a separate connection to the db
local_resources = {
    'etl_postgres': DBClientResource(**get_config().get_postgres_creds()),
    # Reads its warehouse credentials from the same get_clickhouse_creds()
    # the ClickHouseResource does, so dbt cannot drift onto another host.
    'dbt': build_dbt_resource(),
}

global_resources = build_global_resources(get_config().get_io_manager_config(),
                                          get_config().get_clickhouse_creds(),
                                          get_config().get_landing_zone_config())

# N.B. the if the resource keys from the local_resources match the global ones
# they will overwrite the global
all_resources = global_resources | local_resources
