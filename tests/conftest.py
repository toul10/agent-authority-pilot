import pytest

from authz_pilot.world import load_world


@pytest.fixture
def world():
    return load_world()
