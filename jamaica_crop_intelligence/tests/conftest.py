import pytest

from jamaica_crop_intelligence.services.repository import Repository


@pytest.fixture()
def repo():
    r = Repository.create(":memory:", seed=True)
    yield r
    r.close()
