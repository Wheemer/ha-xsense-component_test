"""Run force-arm frontend tests with a mocked DOM and service transport."""

from pathlib import Path
import shutil
import subprocess

import pytest


def test_force_arm_panel_runtime():
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is required for frontend runtime tests")
    script = Path(__file__).with_suffix(".mjs")
    result = subprocess.run(
        [node, "--test", str(script)], capture_output=True, text=True, timeout=30
    )
    assert result.returncode == 0, result.stdout + result.stderr
