# 0.2.69 image association and scheduling

- Retain browser response bodies in a bounded 64 MiB capture buffer (16 MiB per response). Never replay a provider request.
- Collect exact media/project/editor UUID bindings before initial navigation. Keep only bounded UUID tuples in the current browser session.
- Recheck cached bindings while waiting for a detail page; preserve exact project and original media checks.
- Resolve a media UUID to its editor UUID before recovery navigation. Allow the outer recovery timeout to exceed the inner verification timeout.
- Preferred accounts sort first but no longer remove other eligible accounts from round-robin dispatch. Explicit credit-based strategies retain their meanings.
- Record safe underlying response-read exception categories without response bodies, tokens or cookies.

Existing real image 00b16885-65a3-4b98-8d60-49567fcc0dcd was automatically associated and downloaded using only its original media ID. No generation was submitted. The prior manual restoration is not counted as this automatic validation.

This validates recovery of existing images. New paid generation is intentionally not tested in this release.
