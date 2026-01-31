# shared//resources/db_client_resource.py
from __future__ import annotations
from typing import Optional, Dict, Any

import yaml
from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import sessionmaker, Session
import os

class DBClientResource:
    """
    Generic SQLAlchemy DB resource.
    Defaults to PostgreSQL but can be overridden.
    Accepts credentials directly as keyword arguments.
    """

    def __init__(
        self,
        username: str,
        password: str,
        host: str = "localhost",
        port: int = 5432,
        database: str = None,
        driver: str = "postgresql",
        query: Optional[Dict[str, Any]] = None,
    ):
        self.driver = driver
        self.username = username
        self.password = password
        self.host = host
        self.port = port
        self.database = database
        self.query = query or {}

        self._engine: Optional[Engine] = None
        self._SessionLocal: Optional[sessionmaker] = None

    def get_engine(self) -> Engine:
        """Return a SQLAlchemy engine (lazily created)."""
        if self._engine is None:
            self._engine = create_engine(self._build_connection_string(), echo=False, future=True)
        return self._engine

    def get_session(self) -> Session:
        """Return a SQLAlchemy session."""
        if self._SessionLocal is None:
            self._SessionLocal = sessionmaker(bind=self.get_engine(), autocommit=False, autoflush=False)
        return self._SessionLocal()

    def _build_connection_string(self) -> str:
        """Build the SQLAlchemy connection string."""
        if self.driver == "sqlite":
            return f"sqlite:///{self.database or ':memory:'}"

        auth = f"{self.username}:{self.password}@"
        host = f"{self.host}:{self.port}" if self.host and self.port else self.host
        query_str = "?" + "&".join(f"{k}={v}" for k, v in self.query.items()) if self.query else ""
        return f"{self.driver}://{auth}{host}/{self.database or ''}{query_str}"