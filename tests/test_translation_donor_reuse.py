"""Guard reviewed label reuse without treating English equality as bad globally."""

import json
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1] / "custom_components/xsense"
FILES = sorted((ROOT / "translations").glob("*.json"))
ENGLISH = json.loads((ROOT / "translations/en.json").read_text(encoding="utf-8"))

# Explicit semantic pairs, not a reverse lookup by English text. Status values
# may name the same diagnostic condition, but unrelated settings must not merge.
DONORS = {
    "number.camera_alarm_volume.name": "number.alarm_volume.name",
    "number.camera_voice_volume.name": "number.voice_volume.name",
    "sensor.wifi_rssi_level.name": "sensor.wifi_rssi.name",
    "sensor.camera_signal_strength.name": "sensor.wifi_rssi.name",
    "binary_sensor.mute.name": "binary_sensor.mute_status.name",
    "binary_sensor.activated.name": "binary_sensor.activate.name",
    "binary_sensor.is_life_end.name": "sensor.device_status.state.end_of_life",
    "sensor.test_time.name": "sensor.last_self_test_time.name",
    "sensor.pir_sensitivity.name": "number.driveway_sensitivity.name",
    "select.camera_motion_sensitivity.name": "number.detection_sensitivity.name",
}

def value(data, path):
    for part in path.split("."):
        data = data[part]
    return data


def leaves(data, prefix=()):
    if isinstance(data, dict):
        return {
            path: result
            for key, child in data.items()
            for path, result in leaves(child, (*prefix, key)).items()
        }
    return {prefix: data}


@pytest.mark.parametrize("path", FILES, ids=lambda p: p.stem)
@pytest.mark.parametrize("target,source", DONORS.items())
def test_reviewed_donor_parity_when_localized(path, target, source):
    local = json.loads(path.read_text(encoding="utf-8"))["entity"]
    english = ENGLISH["entity"]
    assert value(english, target) == value(english, source)
    if value(local, source) != value(english, source):
        assert value(local, target) == value(local, source)


@pytest.mark.parametrize("path", FILES, ids=lambda p: p.stem)
def test_locale_entity_structure_and_nonempty_labels(path):
    local = json.loads(path.read_text(encoding="utf-8"))["entity"]
    assert leaves(local).keys() == leaves(ENGLISH["entity"]).keys()
    assert all(isinstance(v, str) and v.strip() for v in leaves(local).values())


@pytest.mark.parametrize(
    "left,right", [("zh-CN", "zh-Hans"), ("zh-TW", "zh-Hant"), ("nb", "no")]
)
def test_existing_locale_aliases_remain_identical(left, right):
    a = json.loads((ROOT / f"translations/{left}.json").read_text(encoding="utf-8"))
    b = json.loads((ROOT / f"translations/{right}.json").read_text(encoding="utf-8"))
    assert a == b


@pytest.mark.parametrize("locale", ["de", "fr"])
def test_reviewed_german_french_names_do_not_regress_to_english(locale):
    local = json.loads(
        (ROOT / f"translations/{locale}.json").read_text(encoding="utf-8")
    )["entity"]
    allowed = {"sensor.wifi_ssid"}
    if locale == "de":
        allowed |= {"alarm_control_panel.alarm"}  # Native German, not a gap.
    for domain, items in ENGLISH["entity"].items():
        for key, item in items.items():
            if f"{domain}.{key}" not in allowed:
                assert local[domain][key]["name"] != item["name"], (
                    f"{locale}: {domain}.{key}"
                )


def test_german_end_of_life_refers_to_device_service_life():
    local = json.loads((ROOT / "translations/de.json").read_text(encoding="utf-8"))
    assert (
        local["entity"]["binary_sensor"]["is_life_end"]["name"]
        == "Ende der Lebensdauer"
    )
    assert (
        local["entity"]["sensor"]["device_status"]["state"]["end_of_life"]
        == "Ende der Lebensdauer"
    )
