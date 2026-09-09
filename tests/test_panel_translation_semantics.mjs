import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import vm from "node:vm";

const source = readFileSync(new URL("../custom_components/xsense/frontend/recordings-panel.js", import.meta.url), "utf8");
let Panel;
const context = {
  HTMLElement: class {},
  customElements: { define(_name, component) { Panel = component; } },
};
vm.runInNewContext(source + "\n;globalThis.dictionaries = TRANSLATIONS;", context);
const dictionaries = context.dictionaries;
const english = dictionaries.en;
const tokens = (value) => [...value.matchAll(/\{(\w+)\}/g)].map((match) => match[1]).sort();

test("all 38 dictionaries contain the same 50 keys", () => {
  assert.equal(Object.keys(dictionaries).length, 38);
  assert.equal(Object.keys(english).length, 50);
  for (const dictionary of Object.values(dictionaries)) {
    assert.deepEqual(Object.keys(dictionary).sort(), Object.keys(english).sort());
  }
});

for (const [locale, dictionary] of Object.entries(dictionaries)) {
  for (const [key, value] of Object.entries(english)) {
    test(`${locale}.${key}: nonempty and exact placeholder multiplicity`, () => {
      assert.equal(typeof dictionary[key], "string");
      assert.ok(dictionary[key].trim());
      assert.deepEqual(tokens(dictionary[key]), tokens(value));
    });
  }
}

function panel(locale) {
  const instance = Object.create(Panel.prototype);
  instance.language = () => locale;
  return instance;
}

test("Hindi t() interpolates offline count and shown/total/state", () => {
  const instance = panel("hi");
  assert.ok(instance.t("offlineCount", { count: 37 }).includes("37"));
  const text = instance.t("recordingsCountStatus", { shown: 13, total: 97, state: "TEST_STATE" });
  for (const value of ["13", "97", "TEST_STATE"]) assert.ok(text.includes(value), text);
  assert.ok(!text.includes("प्लेसहोल्डर"));
});

for (const locale of ["hi", "no"]) {
  test(`${locale} t() interpolates the actual preparation status`, () => {
    const text = panel(locale).t("recordingIsNotReadyStatus", { status: "TEST_PENDING" });
    assert.ok(text.includes("TEST_PENDING"), text);
    assert.doesNotMatch(text, /PLASSHOLDER|प्लेसहोल्डर|\{status\}/);
  });
}

// Only the reviewed audio-specific terms are forbidden, not general recording synonyms.
const videoCases = [
  {
    "locale": "ja",
    "wrong": "録音",
    "right": "録画",
    "keys": [
      "loadingRecordings",
      "newestRecording",
      "noRecordingsReady",
      "preparingRecording",
      "recordingEmpty",
      "recordingIsNotReady",
      "recordingIsNotReadyStatus",
      "recordingsCountStatus",
      "recordingsReady",
      "selectRecordingToPlay",
      "clearAllCache",
      "recordingInUse"
    ]
  },
  {
    "locale": "ko",
    "wrong": "녹음",
    "right": "녹화",
    "keys": [
      "loadingRecordings",
      "newestRecording",
      "noRecordingsForDate",
      "noRecordingsReady",
      "preparingRecording",
      "recordingEmpty",
      "recordingIsNotReady",
      "recordingsCountStatus",
      "recordingsReady",
      "selectRecordingToPlay",
      "clearAllCache",
      "confirmClearAllCache",
      "confirmDeleteClipCache",
      "deleteCachedRecording",
      "recordingInUse"
    ]
  },
  {
    "locale": "zh-CN",
    "wrong": "录音",
    "right": "录像",
    "keys": [
      "loadingRecordings",
      "newestRecording",
      "noRecordingsForDate",
      "noRecordingsReady",
      "preparingRecording",
      "recordingEmpty",
      "recordingIsNotReady",
      "recordingIsNotReadyStatus",
      "recordingsCountStatus",
      "recordingsReady",
      "selectRecordingToPlay",
      "cacheCleared",
      "clearAllCache",
      "confirmClearAllCache",
      "confirmDeleteClipCache",
      "deleteCachedRecording",
      "recordingInUse"
    ]
  },
  {
    "locale": "zh-TW",
    "wrong": "錄音",
    "right": "錄影",
    "keys": [
      "loadingRecordings",
      "newestRecording",
      "noRecordingsForDate",
      "noRecordingsReady",
      "preparingRecording",
      "recordingEmpty",
      "recordingIsNotReady",
      "recordingIsNotReadyStatus",
      "recordingsCountStatus",
      "recordingsReady",
      "selectRecordingToPlay",
      "cacheCleared",
      "clearAllCache",
      "confirmClearAllCache",
      "confirmDeleteClipCache",
      "deleteCachedRecording",
      "recordingInUse"
    ]
  },
  {
    "locale": "el",
    "wrong": "ηχογρ",
    "right": "βιντεοσκοπ",
    "keys": [
      "newestRecording",
      "noRecordingsReady",
      "recordingIsNotReady",
      "recordingsReady",
      "recordingInUse"
    ]
  },
  {
    "locale": "vi",
    "wrong": "ghi âm",
    "right": "video",
    "keys": [
      "newestRecording",
      "preparingRecording",
      "recordingIsNotReady",
      "cacheCleared"
    ]
  }
];
const termForm = (text) => text.toLowerCase().normalize("NFD").replace(/\p{M}/gu, "");
for (const { locale, wrong, right, keys } of videoCases) {
  for (const key of keys) {
    test(`${locale}.${key} refers to video rather than audio`, () => {
      const text = termForm(dictionaries[locale][key]);
      assert.ok(!text.includes(termForm(wrong)), text);
      assert.ok(text.includes(termForm(right)), text);
    });
  }
}

const cameraTerms = {"cs":"Kamera","fr":"Caméra","hu":"Kamera","lt":"Kamera","ro":"Cameră video","sk":"Kamera","vi":"Camera"};
for (const [locale, term] of Object.entries(cameraTerms)) {
  for (const key of ["camera", "genericCamera"]) {
    test(`${locale}.${key} names a video camera`, () => {
      assert.ok(dictionaries[locale][key].toLowerCase().includes(term.toLowerCase()));
    });
  }
}

test("Arabic cache labels and confirmation describe storage, not hiding", () => {
  for (const key of ["cached", "notCached", "confirmDeleteClipCache"]) {
    assert.ok(dictionaries.ar[key].includes("مؤقت"));
    assert.ok(!dictionaries.ar[key].includes("مخب"));
  }
});

test("connection and cache labels describe the actual state", () => {
  assert.equal(panel("zh-TW").t("online"), "已連線");
  assert.equal(panel("pl").t("offline"), "offline");
  assert.ok(dictionaries.ru.notCached.includes("кэше"));
  assert.ok(!dictionaries.ru.notCached.includes("кэшируется"));
  assert.ok(dictionaries.uk.notCached.includes("кеші"));
  assert.ok(!dictionaries.uk.notCached.includes("кешується"));
});
