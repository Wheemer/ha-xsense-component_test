import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";
import vm from "node:vm";

const source = readFileSync(new URL("../custom_components/xsense/frontend/recordings-panel.js", import.meta.url), "utf8");
const clip = { entry_id: "entry-a", serial: "cam-a", start: 100, end: 110, playback_url: "/api/xsense/recording" };

function deferred() {
  let resolve;
  const promise = new Promise((done) => { resolve = done; });
  return { promise, resolve };
}

function panelFixture() {
  let Panel;
  let now = 1000;
  const releases = [];
  const revoked = [];
  const context = vm.createContext({
    HTMLElement: class {
      attachShadow() { this.shadowRoot = { getElementById: () => null }; }
    },
    customElements: { define: (_name, value) => { Panel = value; } },
    AbortController,
    URLSearchParams,
    URL: { createObjectURL: () => "blob:video", revokeObjectURL: (url) => revoked.push(url) },
    Date: class extends Date { static now() { return now; } },
    performance: { now: () => now },
    window: { history: { state: null, replaceState() {} }, location: { hash: "" } },
    document: { createElement: () => ({ canPlayType: () => "" }) },
    console,
  });
  vm.runInContext(source, context);
  const panel = new Panel();
  panel.render = () => {};
  panel.logPanelEvent = () => {};
  panel.clipDebugPayload = () => ({});
  panel._hass = {
    callApi: async (...args) => { releases.push(args); },
    connection: { sendMessagePromise: async () => ({ path: "/signed" }) },
  };
  return { panel, context, releases, revoked, setTime: (value) => { now = value; } };
}

function response({ token = "token-a", ok = true, type = "application/vnd.apple.mpegurl", blob = async () => ({ size: 10 }) } = {}) {
  return {
    ok, status: ok ? 200 : 404, url: "/signed",
    headers: new Map([["X-XSense-Playback-Token", token], ["Content-Location", `/api/xsense/recordings/play/${token}/root.m3u8`], ["content-type", type]]),
    blob,
  };
}

test("signed paths renew before their one-hour expiry", async () => {
  const { panel, setTime } = panelFixture();
  let calls = 0;
  panel._hass.connection.sendMessagePromise = async () => ({ path: `/signed-${++calls}` });
  assert.equal(await panel.signPath("/clip"), "/signed-1");
  setTime(3000000);
  assert.equal(await panel.signPath("/clip"), "/signed-1");
  setTime(3601000);
  assert.equal(await panel.signPath("/clip"), "/signed-2");
  assert.equal(calls, 2);
});

test("ready HLS keeps its token until viewer closure", async () => {
  const { panel, context, releases } = panelFixture();
  context.fetch = async () => response();
  panel.selectedClip = clip;
  await panel.prepareClipPlayback(clip);
  const key = panel.playbackKey(clip);
  assert.equal(panel.playbackUrls.get(key), "/api/xsense/recordings/play/token-a/root.m3u8");
  assert.equal(releases.length, 0);
  await panel.closeViewer();
  assert.equal(panel.playbackUrls.size, 0);
  assert.equal(releases.length, 1);
  assert.match(releases[0][1], /token=token-a/);
});

test("browser Back destroys HLS and releases that viewer's token", async () => {
  const { panel, releases } = panelFixture();
  const key = panel.playbackKey(clip);
  let destroyed = 0;
  panel.selectedClip = clip;
  panel.hlsInstances.set(key, { destroy: () => { destroyed++; } });
  panel.playbackTokens.set(key, { clip, token: "viewer-a" });
  panel.syncRouteFromHash = () => { panel.selectedClip = null; };
  await panel.handleRouteChange();
  assert.equal(destroyed, 1);
  assert.equal(panel.hlsInstances.size, 0);
  assert.equal(releases.length, 1);
  assert.match(releases[0][1], /token=viewer-a/);
});

test("closing before signing completes never starts fetch", async () => {
  const { panel, context } = panelFixture();
  const signed = deferred();
  panel.signPath = () => signed.promise;
  let fetched = false;
  context.fetch = async () => { fetched = true; return response(); };
  const preparation = panel.prepareClipPlayback(clip);
  panel.disposePlaybackResources();
  signed.resolve("/signed");
  await preparation;
  assert.equal(fetched, false);
  assert.equal(panel.playbackUrls.size, 0);
});

test("late fetch after closure releases its token without restoring playback", async () => {
  const { panel, context, releases } = panelFixture();
  const fetched = deferred();
  const entered = deferred();
  let signal;
  context.fetch = (_url, options) => { signal = options.signal; entered.resolve(); return fetched.promise; };
  const preparation = panel.prepareClipPlayback(clip);
  await entered.promise;
  assert.ok(signal);
  panel.disposePlaybackResources();
  assert.equal(signal.aborted, true);
  fetched.resolve(response());
  await preparation;
  assert.equal(panel.playbackUrls.size, 0);
  assert.equal(panel.playbackTokens.size, 0);
  assert.equal(releases.length, 1);
});

test("late blob after closure does not allocate a blob URL", async () => {
  const { panel, context, releases } = panelFixture();
  const blob = deferred();
  const entered = deferred();
  let allocated = 0;
  context.URL.createObjectURL = () => { allocated++; return "blob:late"; };
  context.fetch = async () => response({ type: "video/mp4", blob: () => { entered.resolve(); return blob.promise; } });
  const preparation = panel.prepareClipPlayback(clip);
  await entered.promise;
  panel.disposePlaybackResources();
  blob.resolve({ size: 10 });
  await preparation;
  assert.equal(allocated, 0);
  assert.equal(panel.playbackUrls.size, 0);
  assert.equal(releases.length, 1);
});

test("failed preparation releases its token before retry", async () => {
  const { panel, context, releases } = panelFixture();
  context.fetch = async () => response({ ok: false });
  await panel.prepareClipPlayback(clip);
  assert.equal(releases.length, 1);
  assert.equal(panel.playbackTokens.size, 0);
  context.fetch = async () => response({ token: "retry" });
  await panel.prepareClipPlayback(clip);
  assert.equal(panel.playbackTokens.get(panel.playbackKey(clip)).token, "retry");
});

test("clearing one clip does not release another clip token", () => {
  const { panel, releases } = panelFixture();
  const other = { ...clip, serial: "cam-b" };
  panel.playbackTokens.set(panel.playbackKey(clip), { clip, token: "a" });
  panel.playbackTokens.set(panel.playbackKey(other), { clip: other, token: "b" });
  panel.clearPlaybackUrl(panel.playbackKey(clip));
  assert.equal(releases.length, 1);
  assert.equal(panel.playbackTokens.get(panel.playbackKey(other)).token, "b");
});

test("HLS library arriving after close does not attach to a removed video", async () => {
  const { panel } = panelFixture();
  const library = deferred();
  panel.loadHlsLibrary = () => library.promise;
  const key = panel.playbackKey(clip);
  const video = { dataset: { playbackKey: key, hlsUrl: "/hls" }, canPlayType: () => "" };
  panel.shadowRoot.getElementById = () => video;
  panel.playbackUrls.set(key, "/hls");
  const attach = panel.attachVideoSource(video);
  panel.shadowRoot.getElementById = () => null;
  panel.disposePlaybackResources();
  let created = 0;
  class Hls { constructor() { created++; } static isSupported() { return true; } }
  library.resolve(Hls);
  await attach;
  assert.equal(created, 0);
  assert.equal(panel.hlsInstances.size, 0);
});

test("late fatal error from replaced HLS instance cannot clear current playback", async () => {
  const { panel } = panelFixture();
  const instances = [];
  class Hls {
    static Events = { ERROR: "error" };
    static isSupported() { return true; }
    constructor() { instances.push(this); }
    on(_event, handler) { this.handler = handler; }
    destroy() {}
    attachMedia() {}
    loadSource() {}
  }
  panel.loadHlsLibrary = async () => Hls;
  const key = panel.playbackKey(clip);
  const video = { dataset: { playbackKey: key, hlsUrl: "/hls" }, canPlayType: () => "" };
  panel.shadowRoot.getElementById = () => video;
  panel.playbackUrls.set(key, "/hls");
  await panel.attachVideoSource(video);
  await panel.attachVideoSource(video);
  instances[0].handler("error", { fatal: true, details: "old error" });
  assert.equal(panel.playbackUrls.get(key), "/hls");
  assert.equal(panel.hlsInstances.get(key), instances[1]);
  assert.equal(panel.playbackErrors.size, 0);
});
