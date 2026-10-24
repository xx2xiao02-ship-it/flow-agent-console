"""Read-only model preflight; never submit, retry, or substitute a model."""
from .account_details import FlowAccountReader

IMAGE_MODEL_IDS = frozenset({'NARWHAL', 'BELUGA', 'HARBOR_SEAL', 'GEM_PIX_2'})


class ImageModelError(RuntimeError):
    pass


def catalog_image_models(docs: object) -> set[str]:
    found: set[str] = set()

    def walk(value):
        if isinstance(value, str) and value in IMAGE_MODEL_IDS:
            found.add(value)
        elif isinstance(value, list):
            for item in value:
                walk(item)
    walk(docs)
    return found


async def check_image_model(reader: FlowAccountReader, model: str):
    models = catalog_image_models(await reader.call('HTrJv', []))
    if not models:
        raise ImageModelError('catalog_unread')
    wire = {'lite': 'HARBOR_SEAL', 'pro': 'GEM_PIX_2',
            'standard': 'NARWHAL', 'nano_banana_2': 'NARWHAL'}.get(model.lower(), model.upper())
    if wire not in models:
        raise ImageModelError('narwhal_retired' if wire == 'NARWHAL' and 'BELUGA' in models else 'model_unavailable')
