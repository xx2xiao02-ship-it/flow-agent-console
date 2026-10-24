"""Persist the validation-browser and generation-visibility preferences."""
import json
from fastapi import HTTPException

def snapshot(service):
    with service.connect() as db:
        row = db.execute("select value from console_meta where key='verification_headless'").fetchone()
        gen = db.execute("select value from console_meta where key='generation_browser_visible'").fetchone()
        iso = db.execute("select value from console_meta where key='generation_isolated_desktop'").fetchone()
    # Default is a visible window for both checks and generation; headless and
    # the hidden generation window remain as explicit fallbacks. Generation
    # windows default to the private automation desktop.
    return {'headless': row is not None and row[0] == 'true', 'close_after_check': True,
            'generation_browser_visible': gen is None or gen[0] != 'false',
            'isolated_desktop': iso is None or iso[0] != 'false'}

def configure(service, enabled):
    if service.preparation.running:
        raise HTTPException(409, '账号检查进行中，请结束后修改验证方式')
    with service.connect() as db:
        db.execute("insert or replace into console_meta values ('verification_headless', ?)", (json.dumps(enabled),))
    return snapshot(service)

def configure_generation(service, visible, isolated=None):
    with service.connect() as db:
        db.execute("insert or replace into console_meta values ('generation_browser_visible', ?)", (json.dumps(visible),))
        if isolated is not None:
            db.execute("insert or replace into console_meta values ('generation_isolated_desktop', ?)", (json.dumps(isolated),))
    return snapshot(service)
