"""Execute the shipped panel's asynchronous playback lifecycle with Node."""

from pathlib import Path
import shutil
import subprocess


def test_recordings_panel_runtime():
    node = shutil.which("node")
    assert node is not None, "Install Node.js to run the recordings panel lifecycle checks"
    script = Path(__file__).with_suffix(".mjs")
    result = subprocess.run([node, "--test", str(script)], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
