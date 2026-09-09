import assert from "node:assert/strict";
import { readFileSync, readdirSync } from "node:fs";
import { test } from "node:test";
import vm from "node:vm";

const source = readFileSync(new URL("../custom_components/xsense/frontend/force-arm-panel.js", import.meta.url), "utf8");
const translations = new URL("../custom_components/xsense/translations/", import.meta.url);
const locales = readdirSync(translations).filter((name) => name.endsWith(".json"));

function flatten(value, prefix = "") {
  return Object.fromEntries(Object.entries(value).flatMap(([key, child]) =>
    typeof child === "object"
      ? Object.entries(flatten(child, prefix + key + "."))
      : [[prefix + key, child]]));
}

function fixture({ language = "en", mode = "Away", resources = {}, wsFailure = false, wsPending = false, serviceError = null } = {}) {
  let Panel;
  const calls = [];
  const timers = [];
  const nodes = Object.fromEntries(["h1", "p", "button"].map((key) => [
    key, { textContent: "", addEventListener() {} },
  ]));
  const context = vm.createContext({
    HTMLElement: class { querySelector(key) { return nodes[key]; } },
    customElements: { define(_name, value) { Panel = value; } },
    URLSearchParams,
    window: {
      location: { hash: "#entity_id=alarm_control_panel.base&mode=" + mode, assign() {} },
      history: { length: 2, back() {} },
      setTimeout(callback) { timers.push(callback); return timers.length; },
      clearTimeout() {},
    },
  });
  vm.runInContext(source, context);
  const panel = new Panel();
  const hass = {
    language,
    localize: (key) => key === "ui.common.back" ? "Localized back" : "",
    callWS: () => wsFailure ? Promise.reject(new Error("offline"))
      : wsPending ? new Promise(() => {}) : Promise.resolve({ resources }),
    callService: (...args) => {
      calls.push(args);
      return serviceError ? Promise.reject(serviceError) : Promise.resolve();
    },
  };
  panel.hass = hass;
  panel.connectedCallback();
  return { panel, hass, calls, timers, nodes };
}

const settle = () => new Promise((resolve) => setImmediate(resolve));

for (const file of locales) {
  test("force-arm UI uses backend strings in " + file, async () => {
    const data = JSON.parse(readFileSync(new URL(file, translations), "utf8"));
    const resources = flatten(data, "component.xsense.");
    const { panel, hass, calls, nodes } = fixture({ language: file.slice(0, -5), resources });
    assert.equal(calls.length, 1, "command must not wait for translations");
    await settle();
    assert.equal(nodes.h1.textContent, data.services.force_arm.name);
    assert.ok(nodes.p.textContent.includes(data.services.force_arm.description));
    assert.equal(panel._status, "submitted");
    assert.ok(!nodes.p.textContent.includes("ui.common.done"));
    panel.hass = hass;
    assert.equal(calls.length, 1);
    assert.equal(calls[0][0], "xsense");
    assert.equal(calls[0][1], "force_arm");
    assert.equal(calls[0][2].entity_id, "alarm_control_panel.base");
    assert.equal(calls[0][2].mode, "Away");
  });
}

for (const failure of ["rejected", "unresolved"]) {
  test("translation " + failure + " never blocks or duplicates force arm", async () => {
    const { panel, hass, calls } = fixture({
      wsFailure: failure === "rejected",
      wsPending: failure === "unresolved",
    });
    assert.equal(calls.length, 1);
    await settle();
    panel.hass = hass;
    panel.runAction();
    assert.equal(calls.length, 1);
    assert.equal(panel._status, "submitted");
  });
}

test("invalid mode never sends a security command", async () => {
  const { calls, nodes } = fixture({ mode: "Disarmed" });
  await settle();
  assert.equal(calls.length, 0);
  assert.ok(nodes.p.textContent.includes("invalid"));
});

test("service failure preserves the error and is not a successful arm", async () => {
  const { panel, nodes, timers } = fixture({ serviceError: { message: "Permission denied" } });
  await settle();
  assert.equal(panel._status, "failed");
  assert.equal(nodes.p.textContent, "Permission denied");
  assert.equal(timers.length, 0);
});

test("translated service exceptions substitute placeholders safely", async () => {
  const resources = { "component.xsense.exceptions.force_arm_not_pending.message": "Pas de demande {mode}" };
  const { nodes } = fixture({
    resources,
    serviceError: {
      translation_domain: "xsense",
      translation_key: "force_arm_not_pending",
      translation_placeholders: { mode: "Away" },
    },
  });
  await settle();
  assert.equal(nodes.p.textContent, "Pas de demande Away");
});

test("disconnect clears navigation timer without replaying the command", async () => {
  const { panel, calls } = fixture();
  await settle();
  panel.disconnectedCallback();
  panel.connectedCallback();
  assert.equal(calls.length, 1);
});
