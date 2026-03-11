import http from "node:http";
import { createRequire } from "node:module";

import { WebSocket, WebSocketServer } from "ws";

const require = createRequire(import.meta.url);
const RoonApi = require("node-roon-api");
const RoonApiStatus = require("node-roon-api-status");
const RoonApiTransport = require("node-roon-api-transport");

const DEFAULT_WS_PATH = process.env.BRIDGE_WS_PATH ?? "/events";
const DEFAULT_HOST = process.env.BRIDGE_HOST ?? "127.0.0.1";
const DEFAULT_PORT = Number.parseInt(process.env.BRIDGE_PORT ?? process.env.PORT ?? "8080", 10);
const BRIDGE_SOURCE = process.env.BRIDGE_SOURCE ?? "bridge";
const SHARED_SECRET = (process.env.BRIDGE_SHARED_SECRET ?? "").trim();

const isLoopbackHost = (host) =>
  host === "127.0.0.1" || host === "localhost" || host === "::1";

const ensureSecurityConfiguration = (host, sharedSecret) => {
  if (!isLoopbackHost(host) && !sharedSecret) {
    throw new Error("BRIDGE_SHARED_SECRET is required when BRIDGE_HOST is not loopback");
  }
};

const getRequestUrl = (req) => new URL(req.url ?? "/", `http://${req.headers.host ?? "localhost"}`);

const extractToken = (req) => {
  const header = req.headers["x-hsaj-token"];
  if (typeof header === "string" && header.trim()) {
    return header.trim();
  }
  const queryToken = getRequestUrl(req).searchParams.get("token");
  return queryToken?.trim() || null;
};

const isAuthorized = (req) => {
  if (!SHARED_SECRET) {
    return true;
  }
  return extractToken(req) === SHARED_SECRET;
};

const sendJson = (res, statusCode, payload) => {
  res.writeHead(statusCode, { "Content-Type": "application/json" });
  res.end(JSON.stringify(payload));
};

const normalizeString = (value) => {
  if (value === undefined || value === null) {
    return null;
  }
  const text = String(value).trim();
  return text || null;
};

const parseOptionalInt = (value) => {
  if (value === undefined || value === null || value === "") {
    return null;
  }
  const parsed = Number.parseInt(String(value), 10);
  return Number.isFinite(parsed) ? parsed : null;
};

const toDurationMs = (value) => {
  if (value === undefined || value === null || value === "") {
    return null;
  }
  const numeric = Number.parseFloat(String(value));
  if (!Number.isFinite(numeric)) {
    return null;
  }
  if (numeric > 100000) {
    return Math.round(numeric);
  }
  return Math.round(numeric * 1000);
};

const buildObservedTrackId = ({ zoneId, title, artist, album, durationMs, trackno }) => {
  const identity = [zoneId, title ?? "", artist ?? "", album ?? "", trackno ?? "", durationMs ?? ""]
    .join("|");
  return `observed:${Buffer.from(identity).toString("base64url")}`;
};

const buildHealthResponse = (roonStatus) => ({ status: "ok", roon: roonStatus });

const buildTransportMessage = (payload, source) =>
  JSON.stringify({
    type: "transport_event",
    event: {
      ...payload,
      timestamp: payload.timestamp ?? new Date().toISOString(),
      source: payload.source ?? source,
    },
  });

const buildTrackDetailsFromZone = (zone) => {
  if (!zone?.zone_id) {
    return null;
  }

  const state = normalizeString(zone.state)?.toLowerCase();
  if (state && state !== "playing") {
    return null;
  }

  const nowPlaying = zone.now_playing;
  if (!nowPlaying) {
    return null;
  }

  const title = normalizeString(nowPlaying.title ?? nowPlaying.three_line?.line1);
  const artist = normalizeString(nowPlaying.artist ?? nowPlaying.three_line?.line2);
  const album = normalizeString(nowPlaying.album ?? nowPlaying.three_line?.line3);
  const durationMs = toDurationMs(nowPlaying.length ?? nowPlaying.duration ?? nowPlaying.seek_position);
  const trackno = parseOptionalInt(nowPlaying.track_number ?? nowPlaying.trackno);
  const quality = normalizeString(nowPlaying.format);

  if (!title && !artist && !album) {
    return null;
  }

  const roon_track_id = buildObservedTrackId({
    zoneId: zone.zone_id,
    title,
    artist,
    album,
    durationMs,
    trackno,
  });

  return {
    roon_track_id,
    artist,
    album,
    title,
    duration_ms: durationMs,
    trackno,
    quality,
  };
};

const buildZoneSource = (zoneId, zoneName) => `${BRIDGE_SOURCE}:${zoneName ?? zoneId}`;

const createObservedState = () => {
  /** @type {Map<string, { roon_track_id: string, artist: string | null, album: string | null, title: string | null, duration_ms: number | null, trackno: number | null }>} */
  const tracksById = new Map();
  /** @type {Map<string, string>} */
  const currentTrackByZone = new Map();
  /** @type {Map<string, string>} */
  const zoneNames = new Map();
  /** @type {(payload: object) => void} */
  let broadcast = () => {};

  const rememberTrack = (track) => {
    tracksById.set(track.roon_track_id, {
      roon_track_id: track.roon_track_id,
      artist: track.artist,
      album: track.album,
      title: track.title,
      duration_ms: track.duration_ms,
      trackno: track.trackno,
    });
  };

  const stopZone = (zoneId) => {
    const currentTrackId = currentTrackByZone.get(zoneId);
    if (!currentTrackId) {
      return;
    }
    broadcast({
      event: "track_stop",
      track_id: currentTrackId,
      source: buildZoneSource(zoneId, zoneNames.get(zoneId)),
    });
    currentTrackByZone.delete(zoneId);
  };

  const updateZone = (zone) => {
    const zoneId = normalizeString(zone?.zone_id);
    if (!zoneId) {
      return;
    }

    zoneNames.set(zoneId, normalizeString(zone.display_name) ?? zoneId);
    const track = buildTrackDetailsFromZone(zone);
    if (!track) {
      stopZone(zoneId);
      return;
    }

    rememberTrack(track);
    if (currentTrackByZone.get(zoneId) === track.roon_track_id) {
      return;
    }

    stopZone(zoneId);
    currentTrackByZone.set(zoneId, track.roon_track_id);
    broadcast({
      event: "track_start",
      track_id: track.roon_track_id,
      title: track.title ?? undefined,
      album: track.album ?? undefined,
      artist: track.artist ?? undefined,
      quality: track.quality ?? undefined,
      duration_ms: track.duration_ms ?? undefined,
      trackno: track.trackno ?? undefined,
      source: buildZoneSource(zoneId, zoneNames.get(zoneId)),
    });
  };

  const removeZone = (zone) => {
    const zoneId = normalizeString(zone?.zone_id);
    if (!zoneId) {
      return;
    }
    stopZone(zoneId);
    zoneNames.delete(zoneId);
  };

  const replaceZones = (zones) => {
    const seenZoneIds = new Set();
    zones.forEach((zone) => {
      const zoneId = normalizeString(zone?.zone_id);
      if (!zoneId) {
        return;
      }
      seenZoneIds.add(zoneId);
      updateZone(zone);
    });

    Array.from(currentTrackByZone.keys())
      .filter((zoneId) => !seenZoneIds.has(zoneId))
      .forEach((zoneId) => removeZone({ zone_id: zoneId }));
  };

  const reset = () => {
    Array.from(currentTrackByZone.keys()).forEach((zoneId) => stopZone(zoneId));
    currentTrackByZone.clear();
    zoneNames.clear();
  };

  return {
    getTrack: (roonTrackId) => tracksById.get(roonTrackId) ?? null,
    removeZone,
    replaceZones,
    reset,
    setBroadcaster: (fn) => {
      broadcast = fn;
    },
    updateZone,
  };
};

const startApiServer = (port, host, healthProvider, trackProvider, blockedProvider) => {
  const server = http.createServer((req, res) => {
    const url = getRequestUrl(req);

    if (req.method === "GET" && url.pathname === "/health") {
      sendJson(res, 200, healthProvider());
      return;
    }

    if (!isAuthorized(req)) {
      sendJson(res, 401, { message: "Unauthorized" });
      return;
    }

    if (req.method === "GET" && url.pathname === "/blocked") {
      const blocked = blockedProvider();
      if (blocked === null) {
        sendJson(res, 501, { message: "Blocked endpoint is not implemented" });
        return;
      }
      sendJson(res, 200, blocked);
      return;
    }

    if (req.method === "GET" && url.pathname.startsWith("/track/")) {
      const roonTrackId = decodeURIComponent(url.pathname.replace("/track/", ""));
      const track = trackProvider(roonTrackId);

      if (track) {
        sendJson(res, 200, track);
        return;
      }

      sendJson(res, 404, { message: "Track not found", roon_track_id: roonTrackId });
      return;
    }

    sendJson(res, 404, { message: "Not Found" });
  });

  server.listen(port, host, () => {
    console.log(`Bridge HTTP endpoint listening on http://${host}:${port}`);
  });
  return server;
};

const startWebSocketChannel = (server, path, source) => {
  const wss = new WebSocketServer({
    server,
    path,
    verifyClient: (info, done) => {
      if (isAuthorized(info.req)) {
        done(true);
        return;
      }
      done(false, 401, "Unauthorized");
    },
  });

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

const handleZoneSubscription = (data, observedState) => {
  if (!data || typeof data !== "object") {
    return;
  }

  if (Array.isArray(data.zones)) {
    observedState.replaceZones(data.zones);
    return;
  }

  if (Array.isArray(data.zones_removed)) {
    data.zones_removed.forEach((zone) => observedState.removeZone(zone));
  }
  if (Array.isArray(data.zones_added)) {
    data.zones_added.forEach((zone) => observedState.updateZone(zone));
  }
  if (Array.isArray(data.zones_changed)) {
    data.zones_changed.forEach((zone) => observedState.updateZone(zone));
  }
};

const createRoonClient = (roonConnection, observedState) => {
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
      core.services.RoonApiTransport.subscribe_zones((cmd, data) => {
        console.log(`[transport] ${cmd}`);
        handleZoneSubscription(data, observedState);
      });
    },
    core_unpaired: (core) => {
      roonConnection.status = "disconnected";
      statusService.set_status("Disconnected", "Waiting for Roon Core");
      observedState.reset();
      console.warn(`Lost connection to Roon Core: ${core?.display_name ?? "unknown core"}`);
    },
  });

  statusService = new RoonApiStatus(roon);
  roon.init_services({
    required_services: [RoonApiTransport],
    provided_services: [statusService],
  });
  statusService.set_status("Initializing", "Searching for Roon Core");
  roon.start_discovery();

  return { roon, statusService };
};

const startBridge = () => {
  ensureSecurityConfiguration(DEFAULT_HOST, SHARED_SECRET);

  const roonConnection = { status: /** @type {"connected" | "disconnected"} */ ("disconnected") };
  const observedState = createObservedState();
  const server = startApiServer(
    DEFAULT_PORT,
    DEFAULT_HOST,
    () => buildHealthResponse(roonConnection.status),
    observedState.getTrack,
    () => null,
  );
  const channel = startWebSocketChannel(server, DEFAULT_WS_PATH, BRIDGE_SOURCE);
  observedState.setBroadcaster(channel.broadcastTransportEvent);
  createRoonClient(roonConnection, observedState);

  console.log(`HSAJ bridge started. WS endpoint ws://${DEFAULT_HOST}:${DEFAULT_PORT}${DEFAULT_WS_PATH}`);
};

startBridge();

