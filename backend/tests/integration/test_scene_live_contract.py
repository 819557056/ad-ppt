"""Real gateway contracts. Skipped unless two explicit live-test acknowledgements exist."""
import os
import pytest

from backend.tests.live_scene_contract import ContractError, ContractRun, LiveConfig


@pytest.mark.scene_live
@pytest.mark.requires_service
def test_explicit_scene_gateway_contracts():
    try:
        config = LiveConfig.from_environment(os.environ)
        if config is None:
            pytest.skip('Live Scene contracts require explicit paid-test and upstream spending-cap acknowledgements')
        ContractRun(config).run()
    except ContractError as exc:
        pytest.fail(str(exc), pytrace=False)
