"""Hand-authored PDF/JSON pairs that simulate the historical MinerU shape."""

from pathlib import Path
from typing import Any

import pytest
from fixtures.synthetic.generate_evidence_pair import generate_pair


@pytest.fixture
def pair(tmp_path: Path) -> tuple[Path, Path, dict[str, Any]]:
    return generate_pair(tmp_path / "inputs")
