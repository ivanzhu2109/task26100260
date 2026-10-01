import os

import pytest

from paycomp import CompensationService, ManualClock


@pytest.fixture()
def db_path(tmp_path):
    return os.path.join(tmp_path, "test.db")


@pytest.fixture()
def clock():
    return ManualClock()


@pytest.fixture()
def env(db_path, clock):
    svc = CompensationService(db_path, clock)
    yield svc, clock
    svc.close()
