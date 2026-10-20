from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from camhelpers import encode_frames


@pytest.fixture(scope="session")
def frames_10s() -> list[tuple[bytes, bool, int]]:
    """10 с настоящего H.264 160x120@15, GOP 15."""
    return encode_frames(150)
