"""Bounded MP4 container checks without installing a media runtime.

Checks track types and durations, not codec decodability. Browser preview and
Flow remain responsible for codec support. No file is sent to a network probe.
"""

import math
import struct
from pathlib import Path


def inspect_mp4(path: str | Path, *, max_duration_seconds: float | None = 10) -> dict:
    path = Path(path)
    size = path.stat().st_size
    budget = 4096
    with path.open("rb") as stream:
        def read(offset, length):
            stream.seek(offset)
            data = stream.read(length)
            if len(data) != length:
                raise ValueError("truncated MP4")
            return data

        def boxes(start, end):
            nonlocal budget
            offset = start
            while offset < end:
                budget -= 1
                if budget < 0 or end - offset < 8:
                    raise ValueError("invalid MP4 box")
                length, kind = struct.unpack(">I4s", read(offset, 8))
                header = 8
                if length == 1:
                    length = struct.unpack(">Q", read(offset + 8, 8))[0]
                    header = 16
                if length == 0:
                    length = end - offset
                if length < header or offset + length > end:
                    raise ValueError("invalid MP4 length")
                yield kind, offset + header, offset + length
                offset += length

        def duration(start, end):
            version = read(start, 1)[0]
            if version == 0 and end - start >= 20:
                scale, ticks = struct.unpack(">II", read(start + 12, 8))
                unknown = 0xffffffff
            elif version == 1 and end - start >= 32:
                scale, ticks = struct.unpack(">IQ", read(start + 20, 12))
                unknown = 0xffffffffffffffff
            else:
                raise ValueError("invalid MP4 time header")
            if not scale or ticks == unknown:
                raise ValueError("unknown MP4 duration")
            return ticks / scale

        has_brand = has_media = False
        movie_duration = None
        video_durations = []
        for kind, start, end in boxes(0, size):
            if kind == b"ftyp":
                has_brand = end - start >= 8
            elif kind == b"mdat":
                has_media = end > start
            elif kind == b"moov":
                for child, cstart, cend in boxes(start, end):
                    if child == b"mvhd":
                        movie_duration = duration(cstart, cend)
                    elif child == b"trak":
                        for tkind, tstart, tend in boxes(cstart, cend):
                            if tkind != b"mdia":
                                continue
                            handler = None
                            seconds = None
                            for mkind, mstart, mend in boxes(tstart, tend):
                                if mkind == b"hdlr" and mend - mstart >= 12:
                                    handler = read(mstart + 8, 4)
                                elif mkind == b"mdhd":
                                    seconds = duration(mstart, mend)
                            if handler == b"vide" and seconds is not None:
                                video_durations.append(seconds)
        if not has_brand or not has_media or movie_duration is None or not video_durations:
            raise ValueError("missing MP4 video track")
        seconds = max(movie_duration, *video_durations)
        if not math.isfinite(seconds) or seconds <= 0:
            raise ValueError("invalid MP4 duration")
        if max_duration_seconds is not None and seconds > max_duration_seconds:
            raise ValueError("video exceeds input duration limit")
        return {"duration_seconds": seconds, "size": size}
