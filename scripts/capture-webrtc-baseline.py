"""Generate a wire-order fixture from the user-selected historical commit."""
import asyncio
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import types

COMMIT = "65984b9126deb0646f8fd1b4f756f5430e4718e8"
source = subprocess.check_output([
    "git", "show", f"{COMMIT}:custom_components/xsense/webrtc_signal.py"
], text=True)
assert source.count("from .const import LOGGER") == 1
source = source.replace("from .const import LOGGER", "import logging\nLOGGER = logging.getLogger(__name__)")
module = types.ModuleType("xsense_historical_relay")
sys.modules[module.__name__] = module
exec(compile(source, COMMIT, "exec"), module.__dict__)
spec = importlib.util.spec_from_file_location("baseline_test", "tests/test_webrtc_historical_baseline.py")
test = importlib.util.module_from_spec(spec)
spec.loader.exec_module(test)

async def main():
    traces = {}
    for serial in ("CAMERA_A", "CAMERA_B"):
        traces[serial] = await test.relay_trace(module, serial)
    destination = Path("tests/fixtures/webrtc_65984b9.json")
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps({"commit": COMMIT, "traces": traces}, indent=2) + "\n")
    print(f"Captured {COMMIT} -> {destination}")

asyncio.run(main())
