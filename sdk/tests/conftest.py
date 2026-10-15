import pytest

from gits_sdk.testing import FakeCore


@pytest.fixture
async def core():
    async with FakeCore() as c:
        yield c
