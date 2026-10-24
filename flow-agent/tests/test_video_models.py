import pytest
from pydantic import ValidationError
from flow_agent.console import JobInput,VideoPageCheckInput
from flow_agent.video_models import VIDEO_MODELS

@pytest.mark.parametrize('model',list(VIDEO_MODELS))
def test_models_and_compatible_defaults(model):
    assert JobInput(kind='video',prompt='test',video_model=model).video_model==model
    assert VideoPageCheckInput(video_model=model).video_model==model

@pytest.mark.parametrize('extra',[
 {'duration':'4s'},{'duration':'6s'},{'duration':'10s'},{'quality':'360p'},
 {'video_mode':'edit','source_video_id':'video123'},
 {'video_mode':'references','reference_image_ids':['image123']},
])
def test_veo_quality_rejects_unsupported_before_browser(extra):
    with pytest.raises(ValidationError):
        JobInput(kind='video',prompt='test',video_model='veo_3_1_quality',**extra)

def test_veo_lite_fast_reference_ratio():
    for model in ('veo_3_1_lite','veo_3_1_fast'):
        data=JobInput(kind='video',prompt='test',video_model=model,aspect='9:16',reference_image_ids=['a','b','c','d'])
        assert data.video_mode=='references' and data.reference_image_ids==['a','b','c','d']


@pytest.mark.asyncio
async def test_veo_engine_requires_official_executor(tmp_path):
    from unittest.mock import AsyncMock
    from flow_agent.engine import FlowEngine
    engine=FlowEngine(tmp_path)
    engine._run_json=AsyncMock()
    with pytest.raises(ValueError,match='Veo'):
        await engine.generate_video('fixture',video_model='veo_3_1_fast')
    engine._run_json.assert_not_called()
    runner=AsyncMock();runner.run.return_value={'status':'ok'}
    engine.official_videos=runner
    await engine.generate_video('fixture',video_model='veo_3_1_quality')
    assert runner.run.call_args.kwargs['video_model']=='veo_3_1_quality'
