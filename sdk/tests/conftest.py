import pytest

from glados_module.testing import FakeCore


@pytest.fixture
async def core():
    async with FakeCore() as c:
        yield c
