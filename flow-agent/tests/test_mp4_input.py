import struct
from pathlib import Path
import pytest
from flow_agent.mp4_input import inspect_mp4

ASSETS = Path(__file__).parent / 'assets'


def test_real_mp4_container_and_length():
    assert inspect_mp4(ASSETS / 'source-1s.mp4')['duration_seconds'] == 1
    with pytest.raises(ValueError):
        inspect_mp4(ASSETS / 'source-11s.mp4')
    assert inspect_mp4(ASSETS / 'source-11s.mp4', max_duration_seconds=None)['duration_seconds'] == 11


@pytest.mark.parametrize('data', [b'', b'not mp4', b'\0\0\0\x10ftypisom', b'\0\0\0\x01moov', b'\0\0\0\x07free', b'\0\0\0\x08free' * 4097],
                         ids=['empty', 'text', 'truncated', 'extended', 'small', 'budget'])
def test_corrupt_or_unbounded_mp4_is_refused(tmp_path, data):
    file = tmp_path / 'bad.mp4'
    file.write_bytes(data)
    with pytest.raises(ValueError):
        inspect_mp4(file)


def test_version_one_time_header(tmp_path):
    def box(kind, data):
        return struct.pack('>I4s', len(data) + 8, kind) + data
    time = b'\1' + b'\0' * 19 + struct.pack('>IQ', 1000, 2000)
    payload = box(b'ftyp', b'isom0000') + box(b'mdat', b'bytes') + box(b'moov', box(b'mvhd', time) + box(b'trak', box(b'mdia', box(b'mdhd', time) + box(b'hdlr', b'\0' * 8 + b'vide'))))
    file = tmp_path / 'version1.mp4'
    file.write_bytes(payload)
    assert inspect_mp4(file)['duration_seconds'] == 2
