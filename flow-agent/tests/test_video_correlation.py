"""wait_video_candidate must only claim gallery items carrying this job's IDs."""
import asyncio
import time

import pytest

from flow_agent.official_videos import OfficialVideos, OfficialVideoError


class FakeAlert:
    async def count(self):
        return 0


class FakeAlertFilter:
    def filter(self, has_text):
        return FakeAlert()


class FakeLocator:
    def get_by_role(self, *args, **kwargs):
        return FakeAlertFilter()


class FakePage:
    def get_by_role(self, *args, **kwargs):
        return FakeAlertFilter()


class FakeRunner:
    async def gallery(self, page):
        return self._gallery.pop(0) if len(self._gallery) > 1 else self._gallery[0]

    def __init__(self, galleries):
        self._gallery = galleries


OTHER = ({'https://flow-content.google/video/ffffffff-ffff'}, 'group-other')
MINE = ({'https://flow-content.google/video/aaaaaaaa-bbbb'}, 'group-mine')
IDS = ['aaaaaaaa-bbbb']
# Content ID ≠ editor ID: the gallery carries the content ID while the job
# holds the editor ID; only the captured detail binding links the two.
CONTENT = 'cccccccc-dddd-eeee-ffff-000000000000'
EDITOR = 'aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee'
BINDING_ROWS = {(CONTENT, 'fixture-project', EDITOR)}
CONTENT_SOURCE = ({'https://flow-content.google/video/' + CONTENT}, 'group-content')


class FakeRunnerWithBindings(FakeRunner):
    def __init__(self, galleries):
        super().__init__(galleries)
        self._detail_rows = BINDING_ROWS


@pytest.mark.asyncio
async def test_content_id_via_detail_binding_is_matched():
    """Gallery shows the content ID; the job holds the editor ID — matched."""
    runner = FakeRunnerWithBindings([[CONTENT_SOURCE]])
    report = {'upload_project_id': 'fixture-project'}
    candidate = await OfficialVideos.wait_video_candidate(
        runner, FakePage(), [EDITOR], set(), report, time.monotonic() + 5)
    assert candidate == CONTENT_SOURCE


@pytest.mark.asyncio
async def test_content_id_of_other_project_is_not_matched():
    """A content binding from another project never widens this job's IDs."""
    runner = FakeRunnerWithBindings([[CONTENT_SOURCE]])
    runner._detail_rows = {(CONTENT, 'other-project', EDITOR)}
    report = {'upload_project_id': 'fixture-project'}
    candidate = await OfficialVideos.wait_video_candidate(
        runner, FakePage(), [EDITOR], set(), report, time.monotonic() + 5)
    assert candidate == CONTENT_SOURCE  # evidence only, via the fallback branch


@pytest.mark.asyncio
async def test_unmatched_new_item_serves_as_readiness_evidence_only():
    """Only unmatched new assets exist: usable as evidence, never as identity.

    Identity is enforced afterwards through the project-scoped detail binding,
    so an unmatched item may open the editor; a wrong one can never download.
    """
    runner = FakeRunner([[OTHER]])
    report = {}
    candidate = await OfficialVideos.wait_video_candidate(
        runner, FakePage(), IDS, set(), report, time.monotonic() + 5)
    assert candidate == OTHER


@pytest.mark.asyncio
async def test_matched_item_wins_over_unmatched_new_items():
    """Within one poll, an ID-matched item is preferred over unmatched ones."""
    runner = FakeRunner([[OTHER, MINE]])
    report = {}
    candidate = await OfficialVideos.wait_video_candidate(
        runner, FakePage(), IDS, set(), report, time.monotonic() + 5)
    assert candidate == MINE


class DetailPage:
    """A stable /edit/<uuid> page whose download button never appears."""
    url = f'https://flow.google.com/project/fixture-project/edit/d1e2f3a4-b1c2-4d3e-8f4a-111122223333'

    def is_closed(self):
        return False

    def get_by_role(self, *args, **kwargs):
        class No:
            async def is_visible(self):
                return False
        return No()


@pytest.mark.asyncio
async def test_verify_detail_fails_fast_on_foreign_editor_id():
    """A detail page bound to another clip fails immediately, not at deadline."""
    runner = FakeRunner([[]])
    runner._detail_rows = {(CONTENT, 'fixture-project', EDITOR)}  # binding for OUR clip
    started = time.monotonic()
    with pytest.raises(OfficialVideoError) as exc:
        # deadline far away: the early exit must beat it
        await OfficialVideos.verify_video_detail(
            runner, DetailPage(), {EDITOR}, 'fixture-project',
            lambda: asyncio.sleep(0), time.monotonic() + 30)
    assert exc.value.code == 'video_result_identity_mismatch'
    assert time.monotonic() - started < 5


@pytest.mark.asyncio
async def test_verify_detail_waits_when_no_binding_yet():
    """Without any captured binding the waiter keeps waiting (binding may come)."""
    runner = FakeRunner([[]])
    runner._detail_rows = set()
    started = time.monotonic()
    with pytest.raises(OfficialVideoError):
        await OfficialVideos.verify_video_detail(
            runner, DetailPage(), {EDITOR}, 'fixture-project',
            lambda: asyncio.sleep(0), time.monotonic() + 1)
    assert 0.9 < time.monotonic() - started <= 3  # waited the deadline, did not fail early
