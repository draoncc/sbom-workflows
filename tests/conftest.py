import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = Path(__file__).resolve().parent / "fixtures"

_spec = importlib.util.spec_from_file_location("image_map", ROOT / "scripts" / "image-map.py")
assert _spec and _spec.loader
image_map = importlib.util.module_from_spec(_spec)
sys.modules["image_map"] = image_map
_spec.loader.exec_module(image_map)


@pytest.fixture
def fixtures() -> Path:
    return FIXTURES
