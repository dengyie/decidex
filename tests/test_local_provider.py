import pytest
from decidex.providers.local import LocalNanoJevProvider
from decidex.types import ObservationPayload, PrimitiveType, QuestionSpec


@pytest.mark.asyncio
async def test_local_nano_jev_in_memory_runner_null_confidence():
    """Verifies that in-memory runner handles None confidence safely without TypeError."""
    def mock_runner(req_body):
        return {
            "results": [
                {
                    "id": "act",
                    "selected": "jump",
                    "confidence": None,  # Should fallback to 1.0 without crashing
                    "distribution": {"jump": 0.8, "walk": 0.2}
                }
            ]
        }

    provider = LocalNanoJevProvider(in_memory_runner=mock_runner)
    payload = ObservationPayload(observation={"x": 10})
    questions = [
        QuestionSpec(id="act", primitive=PrimitiveType.CHOICE, description="Choose", options=["jump", "walk"])
    ]

    verdicts = await provider.infer(payload, questions)
    assert len(verdicts) == 1
    assert verdicts[0].selected == "jump"
    assert verdicts[0].raw_confidence == 1.0
    assert verdicts[0].calibrated_confidence == 1.0
    assert verdicts[0].distribution == {"jump": 0.8, "walk": 0.2}


@pytest.mark.asyncio
async def test_local_nano_jev_context_manager():
    """Verifies async context manager lifecycle."""
    async with LocalNanoJevProvider(in_memory_runner=lambda req: {"results": []}) as provider:
        assert provider.in_memory_runner is not None
