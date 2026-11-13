import pytest

import telemetry


@pytest.fixture(autouse=True)
def _reset_telemetry():
    telemetry.reset_telemetry()
    yield
    telemetry.reset_telemetry()
