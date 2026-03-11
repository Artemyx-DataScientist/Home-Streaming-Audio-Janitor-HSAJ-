import fs from "node:fs";

const SUPPORTED_TYPES = new Set(["artist", "album", "track"]);

const normalizeString = (value) => {
  if (value === undefined || value === null) {
    return null;
  }
  const text = String(value).trim();
  return text || null;
};

const parseOptionalInt = (value, field) => {
  if (value === undefined || value === null || value === "") {
    return null;
  }
  const parsed = Number.parseInt(String(value), 10);
  if (!Number.isFinite(parsed)) {
    throw new Error(`Invalid integer for ${field}: ${value}`);
  }
  return parsed;
};

const buildFallbackId = ({ type, artist, album, title, track_number, duration_ms, label }) => {
  const identity = [
    type,
    artist ?? "",
    album ?? "",
    title ?? "",
    track_number ?? "",
    duration_ms ?? "",
    label ?? "",
  ].join("|");
  return `${type}:${Buffer.from(identity).toString("base64url")}`;
};

const buildFallbackLabel = ({ type, artist, album, title, label }) => {
  if (label) {
    return label;
  }
  if (type === "artist") {
    return artist;
  }
  if (type === "album") {
    return [artist, album].filter(Boolean).join(" - ") || album;
  }
  if (type === "track") {
    return [artist, title].filter(Boolean).join(" - ") || title;
  }
  return null;
};

export const normalizeBlockedObject = (payload) => {
  if (!payload || typeof payload !== "object") {
    throw new Error("Blocked payload must be an object");
  }

  const type = normalizeString(payload.type)?.toLowerCase();
  if (!type || !SUPPORTED_TYPES.has(type)) {
    throw new Error(`Unsupported blocked object type: ${payload.type ?? "missing"}`);
  }

  const artist = normalizeString(payload.artist);
  const album = normalizeString(payload.album);
  const title = normalizeString(payload.title);
  const track_number = parseOptionalInt(
    payload.track_number ?? payload.trackno,
    "track_number",
  );
  const duration_ms = parseOptionalInt(payload.duration_ms, "duration_ms");
  const label = buildFallbackLabel({
    type,
    artist,
    album,
    title,
    label: normalizeString(payload.label),
  });
  const id =
    normalizeString(payload.id) ??
    buildFallbackId({
      type,
      artist,
      album,
      title,
      track_number,
      duration_ms,
      label,
    });

  if (type === "artist" && !artist && !label) {
    throw new Error("Artist blocked object requires artist or label");
  }
  if (type === "album" && !album && !label) {
    throw new Error("Album blocked object requires album or label");
  }
  if (type === "track" && !title && !label) {
    throw new Error("Track blocked object requires title or label");
  }

  return {
    type,
    id,
    label,
    artist,
    album,
    title,
    track_number,
    duration_ms,
  };
};

const parseBlockedPayload = (raw) => {
  let parsed;
  try {
    parsed = JSON.parse(raw);
  } catch (error) {
    throw new Error(`Blocked source is not valid JSON: ${error.message}`);
  }

  if (!Array.isArray(parsed)) {
    throw new Error("Blocked source must be a JSON array");
  }

  const normalized = parsed.map((item) => normalizeBlockedObject(item));
  const deduped = new Map();
  normalized.forEach((item) => {
    deduped.set(`${item.type}:${item.id}`, item);
  });
  return Array.from(deduped.values());
};

export const loadBlockedObjects = (env = process.env) => {
  const inlineJson = normalizeString(env.BRIDGE_BLOCKED_JSON);
  if (inlineJson) {
    return parseBlockedPayload(inlineJson);
  }

  const filePath = normalizeString(env.BRIDGE_BLOCKED_FILE);
  if (!filePath) {
    return null;
  }

  let raw;
  try {
    raw = fs.readFileSync(filePath, "utf8");
  } catch (error) {
    throw new Error(`Could not read BRIDGE_BLOCKED_FILE ${filePath}: ${error.message}`);
  }
  return parseBlockedPayload(raw);
};

export const describeBlockedSource = (env = process.env) => {
  const inlineJson = normalizeString(env.BRIDGE_BLOCKED_JSON);
  if (inlineJson) {
    return { configured: true, mode: "inline_json" };
  }
  const filePath = normalizeString(env.BRIDGE_BLOCKED_FILE);
  if (filePath) {
    return { configured: true, mode: "file", file_path: filePath };
  }
  return { configured: false, mode: "unconfigured" };
};

export const createBlockedProvider = (env = process.env) => {
  const provider = () => loadBlockedObjects(env);
  provider.describe = () => describeBlockedSource(env);
  return provider;
};
