"""Published Flow credit estimates, not Gemini API currency prices.

Source reviewed 2026-10-15. Never infer subscription tier from a balance.
Veo estimates do not infer model access; supported options live in video_models.
"""

SOURCE_URL = "https://support.google.com/flow/answer/16526234?hl=zh-Hans"
OMNI_COSTS = {
    "4s": {"360p": 4, "720p": 7},
    "6s": {"360p": 5, "720p": 10},
    "8s": {"360p": 6, "720p": 12},
    "10s": {"360p": 7, "720p": 15},
}
VEO_COSTS = {
    "lite": {"non_ultra": 10, "ultra": 5},
    "fast": {"non_ultra": 20, "ultra": 10},
    "quality": {"non_ultra": 100, "ultra": 100},
}
PRICING = {"source_url": SOURCE_URL, "checked_on": "2026-10-15",
           "omni_flash": OMNI_COSTS, "veo_3_1": VEO_COSTS}


def video_estimates(credits: int | float | None) -> dict:
    def count(cost: int) -> int | None:
        return max(0, int(credits // cost)) if credits is not None else None

    return {
        "omni_flash": {duration: {quality: count(cost) for quality, cost in costs.items()}
                       for duration, costs in OMNI_COSTS.items()},
        "veo_3_1": {model: {tier: count(cost) for tier, cost in costs.items()}
                     for model, costs in VEO_COSTS.items()},
    }
