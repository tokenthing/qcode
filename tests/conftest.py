import os
import sys
from pathlib import Path

import pytest

# Keep tests away from the real ~/.config/qcode and ~/.local/share/qcode.
_tmp_home = Path(__file__).parent / ".tmp_home"
os.environ["QCODE_CONFIG_DIR"] = str(_tmp_home / "config")
os.environ["QCODE_DATA_DIR"] = str(_tmp_home / "data")
sys.path.insert(0, str(Path(__file__).parents[1] / "src"))


@pytest.fixture
def root(tmp_path: Path) -> Path:
    return tmp_path.resolve()
