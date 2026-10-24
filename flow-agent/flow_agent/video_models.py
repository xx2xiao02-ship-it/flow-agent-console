"""Console video capabilities; verify the requested model on the official page."""
from typing import Literal

VideoModel = Literal['omni_flash', 'veo_3_1_lite', 'veo_3_1_fast', 'veo_3_1_quality']
VIDEO_MODELS = {
    'omni_flash': 'Omni 1.1 Flash',
    'veo_3_1_lite': 'Veo 3.1 Lite',
    'veo_3_1_fast': 'Veo 3.1 Fast',
    'veo_3_1_quality': 'Veo 3.1 Quality',
}


def validate_video_model(model, duration, quality, mode='text'):
    if model not in VIDEO_MODELS:
        raise ValueError('未知视频模型，请明确指定 Omni 或 Veo 3.1 档位')
    if model == 'omni_flash':
        return
    if duration != '8s' or quality != '720p':
        raise ValueError('Veo 3.1 当前接入仅支持 8s、720p（当前官方网页固定参数）')
    if mode == 'edit':
        raise ValueError('Veo 3.1 不支持本后台的视频编辑，请使用 Omni')
    if mode == 'references':
        if model == 'veo_3_1_quality':
            raise ValueError('Veo 3.1 Quality 不支持参考图，请使用 Lite / Fast 或首尾帧')
        if duration != '8s':
            raise ValueError('Veo 3.1 参考图模式仅支持 8s')
