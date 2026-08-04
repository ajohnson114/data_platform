from .clickhouse_resource import ClickHouseResource
from .io_managers import build_io_manager
from .landing_zone import build_landing_zone
from .warehouse_loader import build_loader


# I want all of the code locations to use the same warehouse, landing zone and io
# manager setup, so they all build their globals here from their own config
def build_global_resources(io_manager_cfg: dict, clickhouse_cfg: dict, landing_zone_cfg: dict) -> dict:
    clickhouse = ClickHouseResource(**clickhouse_cfg)
    landing_zone = build_landing_zone(landing_zone_cfg)

    return {
        'io_manager': build_io_manager(io_manager_cfg),
        'clickhouse': clickhouse,
        'landing_zone': landing_zone,
        # Built from the other two rather than from its own config block: which
        # loader is valid depends on whether the warehouse can read the zone, so
        # it is derived here instead of being a third independent choice. See
        # build_loader.
        'warehouse_loader': build_loader(landing_zone, clickhouse, landing_zone_cfg),
    }
