"""Observe a single submitted request without submitting again."""
import asyncio
import time

UNCERTAIN_WAIT_SECONDS = 180
POLL_SECONDS = 2

async def observe_uncertain(report, recover):
    from .official_images import OfficialImageError
    report.update(stage='query_original', query_only=True, uncertainty_wait_seconds=UNCERTAIN_WAIT_SECONDS)
    started = time.monotonic()
    deadline = started + UNCERTAIN_WAIT_SECONDS
    report['uncertainty_query_started'] = True
    while time.monotonic() < deadline:
        remaining = deadline - time.monotonic()
        try:
            result = await asyncio.wait_for(recover(deadline), remaining)
            if result is not None:
                report['uncertainty_resolved'] = True
                report['uncertainty_wait_elapsed'] = round(time.monotonic() - started, 2)
                return result
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # A failed read/download never permits a new generation submission.
            report['uncertainty_last_error'] = getattr(exc, 'code', type(exc).__name__)
        remaining = deadline - time.monotonic()
        if remaining > 0:
            await asyncio.sleep(min(POLL_SECONDS, remaining))
    report.update(stage='pending_review', outcome='pending_review',
                  uncertainty_query_completed=True, uncertainty_wait_elapsed=round(time.monotonic()-started, 2))
    raise OfficialImageError('original_result_unknown', submitted=True)
