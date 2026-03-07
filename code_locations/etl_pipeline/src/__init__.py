from dagster import Definitions
from .assets import all_assets
from .jobs import all_jobs
from .resources import all_resources
from .asset_checks import all_asset_checks
from .sensors import all_sensors

defs = Definitions(assets = all_assets,
                   jobs=all_jobs,
                   resources = all_resources,
                   asset_checks=all_asset_checks,
                   sensors=all_sensors)