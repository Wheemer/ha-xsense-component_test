import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";
import vm from "node:vm";

const source = readFileSync(new URL("../custom_components/xsense/frontend/recordings-panel.js", import.meta.url), "utf8");
const clip = { entry_id: "entry", serial: "camera", start: 1788913800, end: 1788913810, date: "2026-09-09", duration: 10, playback_url: "/api/play" };
const data = (generation = 1) => ({ generation, cameras: [{ entry_id: clip.entry_id, serial: clip.serial, dates: [clip.date], clips: [{ ...clip }] }] });
function deferred() {
  let resolve;
  const promise = new Promise((done) => { resolve = done; });
  return { resolve, promise };
}
function fixture() {
  let Panel;
  const context = vm.createContext({
    HTMLElement: class { attachShadow() { this.shadowRoot = { getElementById: () => null, querySelectorAll: () => [] }; } },
    customElements: { define: (_name, value) => { Panel = value; } },
    AbortController, URLSearchParams, Date, Intl, performance,
    window: { location: { hash: "" }, history: { state: null, replaceState() {} }, addEventListener() {}, removeEventListener() {} },
    document: { createElement: () => ({ canPlayType: () => "", remove() {} }) },
  });
  context.window.location.pathname = "/xsense-recordings";
  context.window.location.search = "";
  const navigate = (state, _title, url) => {
    context.window.history.state = state;
    context.window.location.hash = url.includes("#") ? url.slice(url.indexOf("#")) : "";
  };
  context.window.history.pushState = navigate;
  context.window.history.replaceState = navigate;
  vm.runInContext(source, context);
  const panel = new Panel();
  panel.render = () => {};
  panel.logPanelEvent = () => {};
  panel.signVisibleThumbnails = async () => {};
  panel._hass = { config: { time_zone: "UTC" }, locale: { language: "en", time_format: "24", time_zone: "server" }, callApi: async () => data(), connection: { sendMessagePromise: async () => ({ path: "/signed" }) } };
  return { panel, context, Panel };
}

test("generated playback errors follow the current locale and preserve status parameters", () => {
  const { panel } = fixture();
  for (const key of ["recordingEmpty", "recordingIsNotReadyStatus", "hlsPlaybackUnsupported", "couldNotLoadHlsPlayer", "hlsPlaybackFailed"]) {
    panel._hass.language = "en";
    const error = panel.localizedPlaybackError(key, { status: 503 });
    panel.playbackErrors.set(panel.playbackKey(clip), panel.playbackErrorValue(error));
    panel._hass.language = "fr";
    assert.ok(panel.renderViewer(clip).includes(panel.escape(panel.t(key, { status: 503 }))));
    assert.ok(!panel.renderViewer(clip).includes(panel.escape(error.message)));
  }
  assert.equal(panel.playbackErrorValue(new Error("upstream detail")), "upstream detail");
});

test("media-preserving updates fall back to render for a changed source or selection", () => {
  const { panel } = fixture();
  panel.selectedClip = clip;
  const key = panel.playbackKey(clip);
  panel.playbackUrls.set(key, "blob:one");
  const video = { dataset: { playbackKey: key }, getAttribute: () => "blob:one" };
  const messages = {};
  panel.shadowRoot.getElementById = id => id === "viewer-video" ? video : messages;
  let renders = 0, updates = 0;
  panel.render = () => { renders++; };
  panel.renderLocaleChange = () => { updates++; };
  panel.error = "refresh failed";
  panel.renderPlaybackUpdate();
  assert.equal(updates, 1);
  assert.match(messages.innerHTML, /refresh failed/);
  panel.playbackUrls.set(key, "blob:two");
  panel.renderPlaybackUpdate();
  panel.selectedClip = null;
  panel.renderPlaybackUpdate();
  assert.equal(renders, 2);
});

test("latest data request wins and stale failure cannot replace it", async () => {
  const { panel } = fixture();
  const first = deferred(), second = deferred();
  let calls = 0;
  panel._hass.callApi = () => (++calls === 1 ? first : second).promise;
  const a = panel.loadData(), b = panel.loadData();
  second.resolve(data(2));
  assert.equal(await b, true);
  first.resolve(data(1));
  assert.equal(await a, false);
  assert.equal(panel.data.generation, 2);
  assert.equal(panel.loading, false);
});

test("stale request completion does not clear a current loading state", async () => {
  const { panel } = fixture();
  const first = deferred(), second = deferred();
  let calls = 0;
  panel._hass.callApi = () => (++calls === 1 ? first : second).promise;
  const a = panel.loadData(), b = panel.loadData();
  first.resolve(data(1));
  await a;
  assert.equal(panel.loading, true);
  second.resolve(data(2));
  await b;
});

test("data response after detach cannot initiate deep-link playback", async () => {
  const { panel, context } = fixture();
  const pending = deferred();
  panel._hass.callApi = () => pending.promise;
  context.window.location.hash = `#entry_id=entry&serial=camera&start=${clip.start}&end=${clip.end}`;
  let preparations = 0;
  panel.prepareClipPlayback = async () => { preparations++; };
  const load = panel.loadData();
  panel.disconnectedCallback();
  pending.resolve(data());
  assert.equal(await load, false);
  assert.equal(preparations, 0);
  assert.equal(panel.data, null);
  assert.equal(panel.loading, false);
});

test("reconnection refreshes after detach without accepting the old response", async () => {
  const { panel } = fixture();
  const old = deferred();
  panel._hass.callApi = () => old.promise;
  const pending = panel.loadData();
  panel.disconnectedCallback();
  panel._hass.callApi = async () => data(2);
  panel.connectedCallback();
  await new Promise((resolve) => setImmediate(resolve));
  old.resolve(data(1));
  await pending;
  assert.equal(panel.data.generation, 2);
});

test("deletion invalidates old loads and reports a partial cleanup", async () => {
  const { panel } = fixture();
  const old = deferred();
  panel._hass.callApi = () => old.promise;
  const pending = panel.loadData();
  panel.t = (key, params) => JSON.stringify({ key, params });
  panel._hass.callApi = async (method) => method === "DELETE"
    ? { deleted_items: 1, remaining_items: 2, skipped_active: 1 } : data(2);
  await panel.deleteCache("all", "all");
  old.resolve(data(1));
  await pending;
  assert.equal(panel.data.generation, 2);
  assert.deepEqual(JSON.parse(panel.notice), { key: "cacheClearSummary", params: { deleted: "1", remaining: "2" } });
});

test("failed deletion reload never shows the success notice", async () => {
  const { panel } = fixture();
  panel._hass.callApi = async (method) => {
    if (method === "DELETE") return { deleted_items: 1, remaining_items: 0 };
    throw new Error("reload failed");
  };
  await panel.deleteCache("all", "all");
  assert.equal(panel.notice, "");
  assert.equal(panel.error, "reload failed");
  assert.equal(panel.deleting, false);
});

test("overlapping deletions and manual reloads are ignored while deleting", async () => {
  const { panel } = fixture();
  const pending = deferred();
  let deletes = 0;
  panel._hass.callApi = async (method) => { if (method === "DELETE") { deletes++; return pending.promise; } return data(); };
  const operation = panel.deleteCache("all", "all");
  await panel.deleteCache("all", "all");
  assert.equal(await panel.loadData(), false);
  pending.resolve({ deleted_items: 1, remaining_items: 0 });
  await operation;
  assert.equal(deletes, 1);
});

test("structured HTTP conflict displays the existing localized message", async () => {
  const { panel } = fixture();
  panel._hass.callApi = async () => { throw { status_code: 409, message: "Conflict" }; };
  await panel.deleteCache("clip", "entry");
  assert.equal(panel.error, panel.t("recordingInUse"));
});

test("valid and empty routes clear only the missing-route error", () => {
  const { panel, context } = fixture();
  panel.data = data();
  panel.error = "unrelated error";
  context.window.location.hash = "#entry_id=entry&serial=camera&start=1";
  panel.syncRouteFromHash();
  assert.notEqual(panel.routeError, "");
  context.window.location.hash = `#entry_id=entry&serial=camera&start=${clip.start}`;
  panel.syncRouteFromHash();
  assert.equal(panel.routeError, "");
  assert.equal(panel.selectedClip.start, clip.start);
  assert.equal(panel.error, "unrelated error");
  context.window.location.hash = "";
  panel.syncRouteFromHash();
  assert.equal(panel.selectedClip, null);
});

test("HLS script failure can be retried", async () => {
  const { panel, context } = fixture();
  const scripts = [];
  context.document.head = { appendChild: (script) => scripts.push(script) };
  const failed = panel.loadHlsLibrary();
  scripts[0].onerror();
  await assert.rejects(failed);
  const retried = panel.loadHlsLibrary();
  assert.equal(scripts.length, 2);
  context.window.Hls = class {};
  scripts[1].onload();
  assert.equal(await retried, context.window.Hls);
});

test("attachment failure is visible and clears the prepared session", async () => {
  const { panel } = fixture();
  const key = panel.playbackKey(clip);
  const video = { dataset: { playbackKey: key }, play() {} };
  panel.shadowRoot.getElementById = () => video;
  panel.bindVideoDiagnostics = () => {};
  panel.attachVideoSource = async () => { throw new Error("HLS unavailable"); };
  panel.playbackUrls.set(key, "/prepared");
  panel.afterRender();
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(panel.playbackErrors.get(key), "HLS unavailable");
  assert.equal(panel.playbackUrls.has(key), false);
});

test("backend warning is escaped and rendered without hiding controls", async () => {
  const { panel, Panel } = fixture();
  panel._hass.callApi = async () => ({ cameras: [], warning: "Cloud failed <unsafe>" });
  await panel.loadData();
  panel.bindEvents = () => {};
  panel.afterRender = () => {};
  Panel.prototype.render.call(panel);
  assert.match(panel.shadowRoot.innerHTML, /Cloud failed &lt;unsafe&gt;/);
  assert.match(panel.shadowRoot.innerHTML, /id="refresh"/);
});

test("HA timezone regrouping preserves timestamps and ascending date order", () => {
  const { panel } = fixture();
  panel._hass.config.time_zone = "America/St_Johns";
  const original = data();
  original.cameras[0].days_descending = false;
  original.cameras[0].clips.push({ ...clip, start: clip.start + 86400, end: clip.end + 86400 });
  const grouped = panel.groupPanelDates(original);
  assert.equal(grouped.cameras[0].clips[0].date, "2026-09-08");
  assert.equal(original.cameras[0].clips[0].date, "2026-09-09");
  assert.equal(grouped.cameras[0].clips[0].start, clip.start);
  assert.equal(grouped.cameras[0].dates[0], "2026-09-08");
  assert.match(panel.formatTime(clip.start), /22:00:00/);
  panel._hass.locale.time_zone = "local";
  assert.equal(panel.timeZone(), Intl.DateTimeFormat().resolvedOptions().timeZone);
});

test("nested delete keyboard events retain native activation", () => {
  const { panel } = fixture();
  const handlers = {};
  const button = { addEventListener: (type, handler) => { handlers[type] = handler; }, querySelector: () => null };
  panel.shadowRoot.querySelectorAll = () => [button];
  panel.bindEvents();
  let cancelled = false;
  handlers.keydown({ key: "Enter", target: {}, preventDefault: () => { cancelled = true; } });
  assert.equal(cancelled, false);
});

test("cache-clear summary has all 38 translations and intact placeholders", () => {
  const { context } = fixture();
  const translations = vm.runInContext("TRANSLATIONS", context);
  assert.equal(Object.keys(translations).length, 38);
  for (const [locale, dictionary] of Object.entries(translations)) {
    assert.equal(typeof dictionary.cacheClearSummary, "string", locale);
    assert.deepEqual(dictionary.cacheClearSummary.match(/\{\w+\}/g)?.sort(), ["{deleted}", "{remaining}"], locale);
    if (locale !== "en") assert.notEqual(dictionary.cacheClearSummary, translations.en.cacheClearSummary, locale);
  }
});

test("native video errors release playback and expose retry state", () => {
  const { panel } = fixture();
  const handlers = {};
  const video = { error: { message: "Cannot decode recording" },
    addEventListener: (event, callback) => { handlers[event] = callback; } };
  panel.selectedClip = clip;
  panel.shadowRoot.getElementById = () => video;
  panel.playbackUrls.set(panel.playbackKey(clip), "/video");
  panel.bindVideoDiagnostics(video);
  handlers.error();
  assert.equal(panel.playbackUrls.size, 0);
  assert.equal(panel.playbackErrors.get(panel.playbackKey(clip)), "Cannot decode recording");
  assert.match(panel.renderViewer(clip), /id="retry-viewer"/);
});

test("clearing pending playback aborts preparation before it can restore a URL", async () => {
  const { panel } = fixture();
  const signing = deferred();
  panel.signPath = () => signing.promise;
  const pending = panel.prepareClipPlayback(clip);
  const key = panel.playbackKey(clip);
  const controller = panel.playbackRequests.get(key);
  panel.clearPlaybackUrl(key);
  signing.resolve("/signed");
  await pending;
  assert.equal(controller.signal.aborted, true);
  assert.equal(panel.playbackUrls.size, 0);
  assert.equal(panel.playbackLoadingKey, "");
});

test("date grouping assembles ISO keys from parts regardless of locale ordering", () => {
  const { panel, context } = fixture();
  context.Intl = { DateTimeFormat: class {
    constructor(locale, options) { this.formatter = new Intl.DateTimeFormat(locale, options); }
    format() { throw new Error("Do not rely on locale date string ordering"); }
    formatToParts(date) { return this.formatter.formatToParts(date).reverse(); }
  } };
  const start = Date.parse("2025-12-31T23:30:00Z") / 1000;
  const input = { cameras: [{ clips: [{ ...clip, start }, { ...clip, start: start + 3600 }] }] };
  const result = panel.groupPanelDates(input);
  assert.deepEqual(Array.from(result.cameras[0].dates), ["2026-01-01", "2025-12-31"]);
  assert.equal(result.cameras[0].clips[0].date, "2025-12-31");
  assert.equal(input.cameras[0].clips[0].date, clip.date);
});

test("status wording retains reviewed neutral meanings in all 38 locales", () => {
  const { context } = fixture();
  const translations = vm.runInContext("TRANSLATIONS", context);
  const expected = {
    "en": ["No pending recordings", "Not cached: {count}"],
    "as": ["\u0995\u09cb\u09a8\u09cb \u09f0\u09c7\u0995\u09f0\u09cd\u09a1\u09bf\u0982 \u09ac\u09be\u0995\u09c0 \u09a8\u09be\u0987", "\u0995\u09c7\u099b\u09a4 \u09f0\u0996\u09be \u09b9\u09cb\u09f1\u09be \u09a8\u09be\u0987: {count}"],
    "ar": ["\u0644\u0627 \u062a\u0648\u062c\u062f \u062a\u0633\u062c\u064a\u0644\u0627\u062a \u0645\u0639\u0644\u0651\u0642\u0629", "\u063a\u064a\u0631 \u0645\u062e\u0632\u0646\u0629 \u0645\u0624\u0642\u062a\u064b\u0627: {count}"],
    "cs": ["\u017d\u00e1dn\u00e9 \u010dekaj\u00edc\u00ed z\u00e1znamy", "Nen\u00ed v mezipam\u011bti: {count}"],
    "da": ["Ingen afventende optagelser", "Ikke cachelagret: {count}"],
    "de": ["Keine ausstehenden Aufnahmen", "Nicht zwischengespeichert: {count}"],
    "el": ["\u0394\u03b5\u03bd \u03c5\u03c0\u03ac\u03c1\u03c7\u03bf\u03c5\u03bd \u03b5\u03ba\u03ba\u03c1\u03b5\u03bc\u03b5\u03af\u03c2 \u03b5\u03b3\u03b3\u03c1\u03b1\u03c6\u03ad\u03c2", "\u03a7\u03c9\u03c1\u03af\u03c2 \u03c0\u03c1\u03bf\u03c3\u03c9\u03c1\u03b9\u03bd\u03ae \u03b1\u03c0\u03bf\u03b8\u03ae\u03ba\u03b5\u03c5\u03c3\u03b7: {count}"],
    "es": ["No hay grabaciones pendientes", "Sin almacenar en cach\u00e9: {count}"],
    "et": ["Ootel salvestisi pole", "Vahem\u00e4llu salvestamata: {count}"],
    "fa": ["\u0647\u06cc\u0686 \u0636\u0628\u0637\u06cc \u062f\u0631 \u0627\u0646\u062a\u0638\u0627\u0631 \u0646\u06cc\u0633\u062a", "\u0630\u062e\u06cc\u0631\u0647\u200c\u0646\u0634\u062f\u0647 \u062f\u0631 \u062d\u0627\u0641\u0638\u0647 \u067e\u0646\u0647\u0627\u0646: {count}"],
    "fi": ["Ei odottavia tallenteita", "Ei v\u00e4limuistissa: {count}"],
    "fr": ["Aucun enregistrement en attente", "Non mis en cache : {count}"],
    "he": ["\u05d0\u05d9\u05df \u05d4\u05e7\u05dc\u05d8\u05d5\u05ea \u05de\u05de\u05ea\u05d9\u05e0\u05d5\u05ea", "\u05dc\u05d0 \u05e0\u05e9\u05de\u05e8\u05d5 \u05d1\u05de\u05d8\u05de\u05d5\u05df: {count}"],
    "hi": ["\u0915\u094b\u0908 \u0930\u093f\u0915\u0949\u0930\u094d\u0921\u093f\u0902\u0917 \u0932\u0902\u092c\u093f\u0924 \u0928\u0939\u0940\u0902 \u0939\u0948", "\u0915\u0948\u0936 \u092e\u0947\u0902 \u0928\u0939\u0940\u0902 \u0930\u0916\u0940 \u0917\u0908\u0902: {count}"],
    "hr": ["Nema snimki na \u010dekanju", "Nije u predmemoriji: {count}"],
    "hu": ["Nincsenek f\u00fcgg\u0151ben l\u00e9v\u0151 felv\u00e9telek", "Nincs gyors\u00edt\u00f3t\u00e1razva: {count}"],
    "id": ["Tidak ada rekaman tertunda", "Tidak dalam cache: {count}"],
    "it": ["Nessuna registrazione in attesa", "Non nella cache: {count}"],
    "ja": ["\u4fdd\u7559\u4e2d\u306e\u9332\u753b\u306f\u3042\u308a\u307e\u305b\u3093", "\u672a\u30ad\u30e3\u30c3\u30b7\u30e5: {count}\u4ef6"],
    "ko": ["\ub300\uae30 \uc911\uc778 \ub179\ud654 \uc5c6\uc74c", "\uce90\uc2dc\ub418\uc9c0 \uc54a\uc74c: {count}\uac1c"],
    "lt": ["Laukian\u010di\u0173 \u012fra\u0161\u0173 n\u0117ra", "Pod\u0117lyje nesaugoma: {count}"],
    "lv": ["Nav gaido\u0161u ierakstu", "Nav ke\u0161atmi\u0146\u0101: {count}"],
    "nl": ["Geen opnamen in behandeling", "Niet in de cache: {count}"],
    "no": ["Ingen ventende opptak", "Ikke hurtigbufret: {count}"],
    "pl": ["Brak oczekuj\u0105cych nagra\u0144", "Nie w pami\u0119ci podr\u0119cznej: {count}"],
    "pt": ["N\u00e3o h\u00e1 grava\u00e7\u00f5es pendentes", "N\u00e3o armazenadas em cache: {count}"],
    "pt-BR": ["Nenhuma grava\u00e7\u00e3o pendente", "N\u00e3o armazenadas em cache: {count}"],
    "ro": ["Nu exist\u0103 \u00eenregistr\u0103ri \u00een a\u0219teptare", "Nu sunt \u00een cache: {count}"],
    "ru": ["\u041d\u0435\u0442 \u043e\u0436\u0438\u0434\u0430\u044e\u0449\u0438\u0445 \u0437\u0430\u043f\u0438\u0441\u0435\u0439", "\u041d\u0435 \u0432 \u043a\u044d\u0448\u0435: {count}"],
    "sk": ["\u017diadne \u010dakaj\u00face z\u00e1znamy", "Nie je vo vyrovn\u00e1vacej pam\u00e4ti: {count}"],
    "sl": ["Ni \u010dakajo\u010dih posnetkov", "Ni v predpomnilniku: {count}"],
    "sv": ["Inga v\u00e4ntande inspelningar", "Inte cachelagrat: {count}"],
    "th": ["\u0e44\u0e21\u0e48\u0e21\u0e35\u0e01\u0e32\u0e23\u0e1a\u0e31\u0e19\u0e17\u0e36\u0e01\u0e17\u0e35\u0e48\u0e23\u0e2d\u0e14\u0e33\u0e40\u0e19\u0e34\u0e19\u0e01\u0e32\u0e23", "\u0e44\u0e21\u0e48\u0e44\u0e14\u0e49\u0e41\u0e04\u0e0a: {count} \u0e23\u0e32\u0e22\u0e01\u0e32\u0e23"],
    "tr": ["Bekleyen kay\u0131t yok", "\u00d6nbelle\u011fe al\u0131nmam\u0131\u015f: {count}"],
    "uk": ["\u041d\u0435\u043c\u0430\u0454 \u0437\u0430\u043f\u0438\u0441\u0456\u0432 \u0432 \u043e\u0447\u0456\u043a\u0443\u0432\u0430\u043d\u043d\u0456", "\u041d\u0435 \u0432 \u043a\u0435\u0448\u0456: {count}"],
    "vi": ["Kh\u00f4ng c\u00f3 b\u1ea3n ghi \u0111ang ch\u1edd", "Ch\u01b0a l\u01b0u v\u00e0o b\u1ed9 nh\u1edb \u0111\u1ec7m: {count}"],
    "zh-CN": ["\u6ca1\u6709\u5f85\u5904\u7406\u7684\u5f55\u50cf", "\u672a\u7f13\u5b58\uff1a{count} \u6761"],
    "zh-TW": ["\u6c92\u6709\u5f85\u8655\u7406\u7684\u9304\u5f71", "\u672a\u5feb\u53d6\uff1a{count} \u7b46"],
  };
  assert.equal(Object.keys(expected).length, Object.keys(translations).length);
  for (const [locale, [zero, pending]] of Object.entries(expected)) {
    assert.equal(translations[locale].allCaughtUp, zero, locale);
    assert.equal(translations[locale].stillSyncing, pending, locale);
    assert.deepEqual(pending.match(/\{\w+\}/g), ["{count}"]);
  }
});

test("uncached status never implies a download for suppressed or on-demand clips", () => {
  const { panel } = fixture();
  for (const syncEnabled of [true, false]) {
    panel.data = { cameras: [{ clips: [{ ...clip, cached: false, retained: true,
      manual_cache_deleted: true, sync_enabled: syncEnabled }] }] };
    assert.equal(panel.readyDetail(1), "Not cached: 1");
    assert.equal(panel.readyDetail(0), "No pending recordings");
    if (!syncEnabled) assert.equal(panel.syncText({ pending_clips: 1 }), "Not cached: 1");
  }
});

test("opening a valid card clears a previous missing-route banner", async () => {
  const { panel, context } = fixture();
  panel.data = data();
  context.window.location.hash = "#entry_id=entry&serial=camera&start=999";
  panel.syncRouteFromHash();
  assert.notEqual(panel.routeError, "");
  panel.prepareClipPlayback = async () => {};
  await panel.openClip(panel.data.cameras[0].clips[0]);
  assert.equal(panel.routeError, "");
  assert.equal(panel.selectedClip.start, clip.start);
  assert.match(context.window.location.hash, new RegExp(`start=${clip.start}`));
});

test("history navigation during deletion cannot start or queue playback", async () => {
  const { panel, context } = fixture();
  panel.data = data();
  const deletion = deferred();
  panel._hass.callApi = async (method) => method === "DELETE" ? deletion.promise : data();
  let preparations = 0;
  panel.prepareClipPlayback = async () => { preparations++; };
  const clearing = panel.deleteCache("all", "all");
  context.window.location.hash = `#entry_id=entry&serial=camera&start=${clip.start}`;
  await panel.handleRouteChange();
  assert.equal(panel.deleting, true);
  assert.equal(panel.selectedClip, null);
  assert.equal(context.window.location.hash, "");
  assert.equal(preparations, 0);
  deletion.resolve({ deleted_items: 1, remaining_items: 0 });
  await clearing;
  assert.equal(preparations, 0);
  assert.equal(panel.selectedClip, null);
  await panel.openClip(panel.data.cameras[0].clips[0]);
  assert.equal(preparations, 1, "An explicit open after deletion must still work");
});

test("direct preparation and retry are inert while deleting", async () => {
  const { panel } = fixture();
  panel.deleting = true;
  panel.selectedClip = clip;
  let signed = 0;
  panel.signPath = async () => { signed++; return "/signed"; };
  await panel.prepareClipPlayback(clip);
  await panel.retryClipPlayback();
  assert.equal(signed, 0);
  assert.equal(panel.playbackRequests.size, 0);
});

test("refresh removing a clip opened during the request releases its HLS resources", async () => {
  const { panel } = fixture();
  panel.data = data();
  const response = deferred();
  let released = 0;
  let destroyed = 0;
  panel._hass.callApi = async (method) => {
    if (method === "GET") return response.promise;
    released++;
    return {};
  };
  panel.prepareClipPlayback = async (item) => {
    const key = panel.playbackKey(item);
    panel.playbackUrls.set(key, "/hls");
    panel.playbackTypes.set(key, "hls");
    panel.playbackTokens.set(key, { clip: item, token: "viewer-token" });
    panel.hlsInstances.set(key, { destroy: () => { destroyed++; } });
  };
  const loading = panel.loadData();
  await panel.openClip(panel.data.cameras[0].clips[0]);
  response.resolve({ cameras: [] });
  await loading;
  assert.equal(panel.selectedClip, null);
  assert.equal(panel.playbackUrls.size, 0);
  assert.equal(panel.playbackTokens.size, 0);
  assert.equal(panel.hlsInstances.size, 0);
  assert.equal(destroyed, 1);
  assert.equal(released, 1);
});

test("timezone updates change the viewer title without rendering or replacing media", async () => {
  const { panel, context } = fixture();
  panel.data = data();
  panel.selectedClip = clip;
  const title = { textContent: "" };
  const video = {};
  panel.shadowRoot.getElementById = (id) => id === "viewer-time" ? title : id === "viewer-video" ? video : null;
  context.window.location.hash = `#entry_id=entry&serial=camera&start=${clip.start}`;
  const thumbnails = deferred();
  panel.signVisibleThumbnails = () => thumbnails.promise;
  let renders = 0;
  panel.render = () => { renders++; };
  panel.hass = { ...panel._hass, config: { time_zone: "America/St_Johns" } };
  assert.equal(renders, 0);
  assert.match(title.textContent, /22:00:00/);
  thumbnails.resolve();
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(renders, 0, "The delayed thumbnail completion must also preserve playback");
  assert.equal(panel.shadowRoot.getElementById("viewer-video"), video);
});

test("attached profile language and time format updates refresh presentation once", () => {
  const { panel } = fixture();
  panel.data = data();
  let updates = 0;
  panel.renderLocaleChange = () => { updates++; };
  panel.hass = { ...panel._hass, language: "fr", locale: { ...panel._hass.locale, language: "fr", time_format: "12" } };
  assert.equal(updates, 1);
  assert.equal(panel.t("back"), "Retour");
  assert.match(panel.formatClipTime(clip), /AM/);
  panel.hass = { ...panel._hass };
  assert.equal(updates, 1, "Ordinary HA state updates must not rebuild controls");
});

test("profile changes rerender a loading viewer even without a timezone change", () => {
  const { panel, Panel } = fixture();
  panel.data = data();
  panel.selectedClip = clip;
  panel.playbackLoadingKey = panel.playbackKey(clip);
  panel.bindEvents = () => {};
  panel.afterRender = () => {};
  panel.render = () => Panel.prototype.render.call(panel);
  panel.render();
  assert.match(panel.shadowRoot.innerHTML, /Preparing recording/);
  panel.hass = { ...panel._hass, language: "fr", locale: { ...panel._hass.locale, language: "fr" } };
  assert.ok(panel.shadowRoot.innerHTML.includes(panel.escape(panel.t("preparingRecording"))));
  assert.ok(!panel.shadowRoot.innerHTML.includes("Preparing recording"));
});
