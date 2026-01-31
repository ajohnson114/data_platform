from shared.resources.db_client_resource import DBClientResource
from shared.resources import global_resources
from config.config import get_config

# I want each code location to have a separate connection to the db
local_resources = {'etl_postgres': DBClientResource(**get_config().get_postgres_creds())}

# N.B. the if the resource keys from the local_resources match the global ones
# they will overwrite the global 
all_resources = global_resources | local_resources