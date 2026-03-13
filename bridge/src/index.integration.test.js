import test from "node:test";
import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { WebSocket } from "ws";


const waitForHttp = async (url, attempts = 40, init = undefined) => {
  for (let index = 0; index < attempts; index += 1) {
    try {
      const response = await fetch(url, init);
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


const waitForWsUnauthorized = async (url) =>
  new Promise((resolve, reject) => {
    const socket = new WebSocket(url);
    const timeout = setTimeout(() => {
      socket.terminate();
      reject(new Error(`Timed out waiting for unauthorized websocket response from ${url}`));
    }, 3000);
    socket.once("unexpected-response", (_request, response) => {
      clearTimeout(timeout);
      socket.terminate();
      resolve(response.statusCode);
    });
    socket.once("open", () => {
      clearTimeout(timeout);
      socket.close();
      reject(new Error("Expected websocket authorization failure"));
    });
    socket.once("error", () => {
      // ws emits an error after unexpected-response; the HTTP status is asserted there.
    });
  });


const waitForWsOpen = async (url) =>
  new Promise((resolve, reject) => {
    const socket = new WebSocket(url);
    const timeout = setTimeout(() => {
      socket.terminate();
      reject(new Error(`Timed out opening websocket ${url}`));
    }, 3000);
    socket.once("open", () => {
      clearTimeout(timeout);
      socket.close();
      resolve();
    });
    socket.once("error", (error) => {
      clearTimeout(timeout);
      reject(error);
    });
  });


const waitForWsOpenWithHeaders = async (url, headers) =>
  new Promise((resolve, reject) => {
    const socket = new WebSocket(url, { headers });
    const timeout = setTimeout(() => {
      socket.terminate();
      reject(new Error(`Timed out opening websocket ${url}`));
    }, 3000);
    socket.once("open", () => {
      clearTimeout(timeout);
      socket.close();
      resolve();
    });
    socket.once("error", (error) => {
      clearTimeout(timeout);
      reject(error);
    });
  });


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
    assert.equal(livePayload.blocked_contract_version, "v2");

    const readyResponse = await waitForHttp(`http://127.0.0.1:${port}/ready`);
    assert.equal(readyResponse.status, 200);
    const readyPayload = await readyResponse.json();
    assert.equal(readyPayload.status, "ready");

    const healthResponse = await waitForHttp(`http://127.0.0.1:${port}/health`);
    assert.equal(healthResponse.status, 200);
    const healthPayload = await healthResponse.json();
    assert.equal(healthPayload.contract_version, "v1");
    assert.equal(healthPayload.blocked_contract_version, "v2");
    assert.equal(healthPayload.blocked_source.configured, true);
    assert.equal(healthPayload.blocked_source.contract_version, "v2");
    assert.equal(healthPayload.blocked_source.health_status, "ok");

    const metricsResponse = await waitForHttp(`http://127.0.0.1:${port}/metrics`);
    assert.equal(metricsResponse.status, 200);
    const metricsPayload = await metricsResponse.text();
    assert.match(metricsPayload, /hsaj_bridge_transport_events_total/);
    assert.match(metricsPayload, /hsaj_bridge_blocked_items/);
    assert.match(metricsPayload, /hsaj_bridge_ready 1/);

    const blockedResponse = await waitForHttp(`http://127.0.0.1:${port}/blocked`);
    assert.equal(blockedResponse.status, 200);
    const blockedPayload = await blockedResponse.json();
    assert.equal(blockedPayload.contract_version, "v2");
    assert.ok(Array.isArray(blockedPayload.items));
  } finally {
    child.kill("SIGTERM");
  }
});


test("bridge enforces shared secret on blocked HTTP and websocket routes", async () => {
  const port = String(19580 + Math.floor(Math.random() * 500));
  const secret = "bridge-secret";
  const child = spawn("node", ["src/index.js"], {
    cwd: process.cwd(),
    env: {
      ...process.env,
      BRIDGE_HOST: "127.0.0.1",
      BRIDGE_PORT: port,
      BRIDGE_BLOCKED_JSON: "[]",
      BRIDGE_SHARED_SECRET: secret,
    },
    stdio: "ignore",
  });

  try {
    await waitForHttp(`http://127.0.0.1:${port}/live`);

    const blockedUnauthorized = await fetch(`http://127.0.0.1:${port}/blocked`);
    assert.equal(blockedUnauthorized.status, 401);

    const blockedAuthorized = await fetch(`http://127.0.0.1:${port}/blocked`, {
      headers: { "X-HSAJ-Token": secret },
    });
    assert.equal(blockedAuthorized.status, 200);

    const unauthorizedStatus = await waitForWsUnauthorized(`ws://127.0.0.1:${port}/events`);
    assert.equal(unauthorizedStatus, 401);

    await waitForWsOpen(`ws://127.0.0.1:${port}/events?token=${secret}`);
    await waitForWsOpenWithHeaders(`ws://127.0.0.1:${port}/events`, {
      "X-HSAJ-Token": secret,
    });
  } finally {
    child.kill("SIGTERM");
  }
});


test("bridge ready probe degrades when live blocked browse source is unavailable", async () => {
  const port = String(19880 + Math.floor(Math.random() * 500));
  const child = spawn("node", ["src/index.js"], {
    cwd: process.cwd(),
    env: {
      ...process.env,
      BRIDGE_HOST: "127.0.0.1",
      BRIDGE_PORT: port,
      BRIDGE_BLOCKED_SOURCE: "roon_browse",
      BRIDGE_BLOCKED_BROWSE_SPECS: JSON.stringify([
        { path: ["Hidden Artists"], object_type: "artist" },
      ]),
    },
    stdio: "ignore",
  });

  try {
    await waitForHttp(`http://127.0.0.1:${port}/live`);
    const healthResponse = await fetch(`http://127.0.0.1:${port}/health`);
    assert.equal(healthResponse.status, 503);
    const healthPayload = await healthResponse.json();
    assert.equal(healthPayload.status, "degraded");
    assert.equal(healthPayload.blocked_source.health_status, "degraded");

    const readyResponse = await fetch(`http://127.0.0.1:${port}/ready`);
    assert.equal(readyResponse.status, 503);
    const readyPayload = await readyResponse.json();
    assert.equal(readyPayload.status, "not_ready");
    assert.equal(readyPayload.blocked_source.mode, "roon_browse_live");
  } finally {
    child.kill("SIGTERM");
  }
});
