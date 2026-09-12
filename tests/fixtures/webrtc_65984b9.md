# Live signaling regression baseline

Baseline: `65984b9126deb0646f8fd1b4f756f5430e4718e8` (`v1.3.12.10`).
Source: `custom_components/xsense/webrtc_signal.py` at that revision.

Issue #182's June 25 ten-switch success follows the announcement of this
release's answer-gated trickled ICE change:
- https://github.com/Jarnsen/ha-xsense-component_test/issues/182#issuecomment-4801049469
- https://github.com/Jarnsen/ha-xsense-component_test/issues/182#issuecomment-4800345651

`webrtc_65984b9.json` records output produced by that historical implementation,
not output from the current helper. The fixture covers URL/Host/SNI options,
unrelated-peer rejection, the offer and embedded ICE, trickled ICE held until
answer, peer exit/rejoin, and closing a session. The regression test runs ten
alternating simulated camera sessions against those captured outputs.

Regenerate from the repository root with its Python dependencies installed:

```sh
PYTHONPATH=. python scripts/capture-webrtc-baseline.py
```

The generator loads the exact commit above and substitutes only the historical
logger import. Do not regenerate expected output from HEAD or silently change
the commit to bless a new implementation. Keep future deviations explicit.

These are protocol simulations, not hardware validation. They do not establish
why a cloud camera might fail to join, reproduce frontend subscription cleanup,
or prove that a specific user's live feed has returned. Identity/scoping and
HA adapter lifecycle are covered by separate tests.
