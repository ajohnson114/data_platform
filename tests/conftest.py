"""
Shared test fixtures and the import helper the whole suite runs on.

WHY MODULES ARE LOADED BY PATH.
The units under test live in packages that are not installed into the test
environment, and whose __init__ files pull in the world -- importing
`shared.resources.landing_zone` the ordinary way executes
`shared/resources/__init__.py`, which imports dagster and dagster_aws. The
modules themselves need none of that: landing_zone needs pandas,
warehouse_loader and guardrails need nothing but the standard library.

Loading the file directly keeps the test dependencies to what the code under
test actually uses, which is what lets CI run the unit suite in seconds on a
bare runner rather than installing three code locations to test a regex.
"""
import importlib.util
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]


def load_module(relative_path: str, name: str | None = None):
    """
    Import a source file directly, bypassing its package __init__.

    `relative_path` is relative to the repository root. Modules are cached under
    `name` so two test files asking for the same unit share one instance.
    """
    path = REPO_ROOT / relative_path
    if not path.is_file():
        raise FileNotFoundError(f"no module at {path}")

    name = name or path.stem
    if name in sys.modules:
        return sys.modules[name]

    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    # Registered before exec so a module importing itself by name resolves.
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="session")
def repo_root() -> Path:
    return REPO_ROOT
