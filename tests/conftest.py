import copy
import pytest
from src.target_app.app import INITIAL_MEMBERS
import src.target_app.app as app_module


@pytest.fixture(autouse=True)
def reset_mock_db():
    """Ensures each test runs with fresh in-memory mock banking data."""
    app_module.MEMBERS_DB = copy.deepcopy(INITIAL_MEMBERS)
    yield
    app_module.MEMBERS_DB = copy.deepcopy(INITIAL_MEMBERS)
