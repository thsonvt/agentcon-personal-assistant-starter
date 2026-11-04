"""Golden matrices generated independently with bundled ReportLab QR encoder.

Version 5-L, byte mode, mask 0. Production does not depend on ReportLab.
"""
import hashlib
import xml.etree.ElementTree as ET

import pytest


@pytest.mark.parametrize('payload,expected', [
    ('https://pippatime.com/people/42', '9ff2129d596837eee8970956e5e618b79c033af141b7b36bc94a03926c5b22c4'),
    ('https://pippatime.com/events/ai-tinkerers-sg-oct-10', 'b6332abab10fab61ae99f53ce74ed17f877edb02a362b4fd3d0c074a96275f06'),
    ('a' * 106, 'e5a3ae4b06533121293aff6261d5654b54c01c81066aba7b1918e0978166dc1e'),
])
def test_matrix_matches_independent_encoder(payload, expected):
    from app.event_qr import qr_matrix
    matrix = qr_matrix(payload)
    assert len(matrix) == 37 and all(len(row) == 37 for row in matrix)
    assert hashlib.sha256(bytes(int(c) for row in matrix for c in row)).hexdigest() == expected


def test_event_qr_is_local_svg_with_quiet_zone():
    from app.event_qr import qr_svg
    svg = qr_svg('https://pippatime.com/people/42')
    root = ET.fromstring(svg)
    assert root.attrib['viewBox'] == '0 0 45 45'
    assert 'https://' not in svg and 'script' not in svg and 'image' not in svg


def test_qr_rejects_overlong_or_non_ascii_payload():
    from app.event_qr import qr_svg
    for payload in ['a' * 107, '☃', '']:
        with pytest.raises(ValueError):
            qr_svg(payload)
