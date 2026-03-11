import test from "node:test";
import assert from "node:assert/strict";
import { spawn } from "node:child_process";


const waitForHttp = async (url, attempts = 40) => {
  for (let index = 0; index < attempts; index += 1) {
    try {
      const response = await fetch(url);
      if (response.ok) {
        return response;
      }
    } catch {
      // server is still starting
    }
    await new Promise((resolve) => setTimeout(resolve, 150));
  }
  throw new Error(`Timed out waiting for ${url}`);
};


test("bridge exposes live, ready, health, and metrics endpoints", async () => {
  const port = String(19080 + Math.floor(Math.random() * 500));
  const child = spawn("node", ["src/index.js"], {
    cwd: process.cwd(),
    env: {
      ...process.env,
      BRIDGE_HOST: "127.0.0.1",
      BRIDGE_PORT: port,
      BRIDGE_BLOCKED_JSON: "[]",
    },
    stdio: "ignore",
  });

  try {
    const liveResponse = await waitForHttp(`http://127.0.0.1:${port}/live`);
    assert.equal(liveResponse.status, 200);
    const livePayload = await liveResponse.json();
    assert.equal(livePayload.status, "live");

    const readyResponse = await waitForHttp(`http://127.0.0.1:${port}/ready`);
    assert.equal(readyResponse.status, 200);
    const readyPayload = await readyResponse.json();
    assert.equal(readyPayload.status, "ready");

    const healthResponse = await waitForHttp(`http://127.0.0.1:${port}/health`);
    assert.equal(healthResponse.status, 200);
    const healthPayload = await healthResponse.json();
    assert.equal(healthPayload.contract_version, "v1");
    assert.equal(healthPayload.blocked_source.configured, true);

    const metricsResponse = await waitForHttp(`http://127.0.0.1:${port}/metrics`);
    assert.equal(metricsResponse.status, 200);
    const metricsPayload = await metricsResponse.text();
    assert.match(metricsPayload, /hsaj_bridge_transport_events_total/);
  } finally {
    child.kill("SIGTERM");
  }
});
