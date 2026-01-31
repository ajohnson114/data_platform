# config/config.py
from __future__ import annotations
import os
import yaml
from typing import Dict, Any

# -------------------------
# Determine base paths
# -------------------------
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

CONFIG_DIR = os.path.join(BASE_DIR, "config")
SECRETS_DIR = os.path.join(BASE_DIR, "secrets")

_ConfigInstance: _Config | None = None

def get_config() -> _Config:
    global _ConfigInstance
    if _ConfigInstance is None:
        _ConfigInstance = _Config()
    return _ConfigInstance

class _Config:
    def __init__(self, env: str | None = None):
        self.env = env or os.getenv("ENV", "dev")

        config_path = os.path.join(CONFIG_DIR, f"config.{self.env}.yaml")
        secrets_path = os.path.join(SECRETS_DIR, f"secrets.{self.env}.yaml")

        if not os.path.exists(config_path):
            raise FileNotFoundError(f"Config file not found: {config_path}")
        if not os.path.exists(secrets_path):
            raise FileNotFoundError(f"Secrets file not found: {secrets_path}")
        
        print('#'*80)
        print(f"Currently using this config: {config_path}")
        print(f"Currently using these secrets: {secrets_path}")
        print('#'*80)

        with open(config_path, "r") as f:
            self.config: Dict[str, Any] = yaml.safe_load(f)
        with open(secrets_path, "r") as f:
            self.secrets: Dict[str, Any] = yaml.safe_load(f)

    def get_postgres_creds(self) -> dict:
        return {'username':self.secrets['dbs']['postgres']['user'],
                'password':self.secrets['dbs']['postgres']['password'],
                'host':self.config['dbs']['postgres']['host'],
                'port':self.config['dbs']['postgres']['port'],
                'database':self.config['dbs']['postgres']['db']
                }
    
    def get_etl_table_ddl(self):
        return self.config['data_pipeline']['postgres']['make_etl_table']
    
    def get_ml_table_ddl(self):
        return self.config['data_pipeline']['postgres']['make_ml_table']
    
    def get_ml_table_name(self):
        return self.config['data_pipeline']['postgres']['ml_table_name']
    
    def get_etl_table_name(self):
        return self.config['data_pipeline']['postgres']['etl_table_name']
    
    def get_cols_required_to_not_have_nulls(self):
        return self.config['data_pipeline']['asset_checks']['cols_required_to_not_have_nulls']
    
