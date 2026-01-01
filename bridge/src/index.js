import http from "node:http";
import { createRequire } from "node:module";

import { WebSocket, WebSocketServer } from "ws";

const require = createRequire(import.meta.url);
const RoonApi = require("node-roon-api");
const RoonApiStatus = require("node-roon-api-status");

const DEFAULT_WS_PATH = process.env.BRIDGE_WS_PATH ?? "/events";
const DEFAULT_WS_INTERVAL_MS = Number.parseInt(process.env.BRIDGE_DEMO_INTERVAL ?? "15000", 10);
const BRIDGE_SOURCE = process.env.BRIDGE_SOURCE ?? "bridge";

/**
 * @typedef {"connected" | "disconnected"} RoonConnectionState
 */

/**
 * @typedef {Object} HealthResponse
 * @property {"ok"} status
 * @property {RoonConnectionState} roon
 */

/**
 * @param {RoonConnectionState} roonStatus
 * @returns {HealthResponse}
 */
const buildHealthResponse = (roonStatus) => ({ status: "ok", roon: roonStatus });

/**
 * @typedef {Object} TransportEventPayload
 * @property {"track_start" | "track_stop"} event
 * @property {string} track_id
 * @property {string} [title]
 * @property {string} [album]
 * @property {string} [artist]
 * @property {string} [quality]
 * @property {number} [duration_ms]
 * @property {string} [timestamp]
 * @property {string} [source]
 */

/**
 * @param {TransportEventPayload} payload
 * @param {string} source
 * @returns {string}
 */
const buildTransportMessage = (payload, source) =>
  JSON.stringify({
    type: "transport_event",
    event: {
      ...payload,
      timestamp: payload.timestamp ?? new Date().toISOString(),
      source: payload.source ?? source,
    },
  });

/**
 * @param {http.Server} server
 * @param {string} path
 * @param {string} source
 */
const startWebSocketChannel = (server, path, source) => {
  const wss = new WebSocketServer({ server, path });

  const broadcastTransportEvent = (payload) => {
    const message = buildTransportMessage(payload, source);
    wss.clients.forEach((client) => {
      if (client.readyState === WebSocket.OPEN) {
        client.send(message);
      }
    });
    console.log(`[ws] transport_event broadcasted for track ${payload.track_id}`);
  };

  wss.on("connection", () => {
    console.log(`[ws] client subscribed to ${path}`);
  });

  return { broadcastTransportEvent };
};

/**
 * @param {TransportEventPayload} track
 * @returns {TransportEventPayload}
 */
const normalizeTrackPayload = (track) => ({
  ...track,
  event: "track_start",
});

/**
 * @param {(payload: TransportEventPayload) => void} broadcaster
 */
const createTransportEmitter = (broadcaster) => {
  /** @type {string | null} */
  let currentTrackId = null;

  /**
   * @param {TransportEventPayload} track
   * @returns {void}
   */
  const trackStart = (track) => {
    if (!track.track_id) {
      return;
    }
    if (currentTrackId === track.track_id) {
      return;
    }
    currentTrackId = track.track_id;
    broadcaster(normalizeTrackPayload(track));
  };

  /**
   * @param {"track_stop" | "track_start"} reason
   * @returns {void}
   */
  const stopCurrentTrack = (reason = "track_stop") => {
    if (!currentTrackId) {
      return;
    }
    broadcaster({
      event: reason,
      track_id: currentTrackId,
    });
    currentTrackId = null;
  };

  return { trackStart, stopCurrentTrack };
};

/**
 * @param {(track: TransportEventPayload) => void} trackStart
 * @returns {NodeJS.Timeout}
 */
const startDemoTransportFeed = (trackStart) => {
  const demoTracks = [
    {
      track_id: "demo-track-1",
      title: "Demo Track 1",
      artist: "HSAJ Dev",
      album: "Bridge Demo",
      quality: "lossless",
      duration_ms: 180_000,
    },
    {
      track_id: "demo-track-2",
      title: "Demo Track 2",
      artist: "HSAJ Dev",
      album: "Bridge Demo",
      quality: "atmos",
      duration_ms: 200_000,
    },
  ];

  let currentIndex = 0;
  trackStart(demoTracks[currentIndex]);
  return setInterval(() => {
    currentIndex = (currentIndex + 1) % demoTracks.length;
    trackStart(demoTracks[currentIndex]);
  }, DEFAULT_WS_INTERVAL_MS);
};

/**
 * @param {{ status: RoonConnectionState }} roonConnection
 * @returns {{ roon: unknown, statusService: unknown }}
 */
const createRoonClient = (roonConnection) => {
  /** @type {ReturnType<typeof RoonApiStatus>} */
  let statusService;

  const roon = new RoonApi({
    extension_id: "com.hsaj.bridge",
    display_name: "HSAJ Bridge",
    display_version: "0.1.0",
    publisher: "HSAJ",
    email: "opensource@hsaj.local",
    website: "https://example.com",
    core_paired: (core) => {
      roonConnection.status = "connected";
      statusService.set_status("OK", `Connected to ${core.display_name}`);
      console.log(`Connected to Roon Core: ${core.display_name} (${core.core_id})`);
    },
    core_unpaired: () => {
      roonConnection.status = "disconnected";
      statusService.set_status("Disconnected", "Waiting for Roon Core");
      console.warn("Lost connection to Roon Core. Waiting for re-connection...");
    },
  });

  statusService = new RoonApiStatus(roon);
  roon.init_services({ provided_services: [statusService] });
  statusService.set_status("Initializing", "Searching for Roon Core");
  roon.start_discovery();

  return { roon, statusService };
};

/**
 * @param {number} port
 * @param {() => HealthResponse} healthProvider
 * @returns {http.Server}
 */
const startHealthServer = (port, healthProvider) => {
  const server = http.createServer((req, res) => {
    const url = req.url ? new URL(req.url, `http://${req.headers.host}`) : null;

    if (req.method === "GET" && url?.pathname === "/health") {
      const response = healthProvider();
      res.writeHead(200, { "Content-Type": "application/json" });
      res.end(JSON.stringify(response));
      return;
    }

    res.writeHead(404, { "Content-Type": "application/json" });
    res.end(JSON.stringify({ message: "Not Found" }));
  });

  server.listen(port, () => {
    console.log(`Health endpoint listening on :${port}/health`);
  });
  return server;
};

/**
 * Стартует dev-экземпляр bridge и подключает Roon.
 * @returns {void}
 */
const startBridge = () => {
  const port = Number.parseInt(process.env.BRIDGE_PORT ?? process.env.PORT ?? "8080", 10);
  const wsPath = process.env.BRIDGE_WS_PATH ?? DEFAULT_WS_PATH;
  const roonConnection = { status: /** @type {RoonConnectionState} */ ("disconnected") };

  createRoonClient(roonConnection);
  const server = startHealthServer(port, () => buildHealthResponse(roonConnection.status));
  const channel = startWebSocketChannel(server, wsPath, BRIDGE_SOURCE);
  const emitter = createTransportEmitter(channel.broadcastTransportEvent);

  if (process.env.BRIDGE_DISABLE_DEMO !== "1") {
    startDemoTransportFeed(emitter.trackStart);
  }

  console.log(`HSAJ bridge dev server started. WS endpoint ws://localhost:${port}${wsPath}`);
};

startBridge();
