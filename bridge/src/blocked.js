import fs from "node:fs";

const SUPPORTED_TYPES = new Set(["artist", "album", "track"]);
export const BLOCKED_CONTRACT_VERSION = "v2";
const DEFAULT_CACHE_SECONDS = 60;

const normalizeString = (value) => {
  if (value === undefined || value === null) {
    return null;
  }
  const text = String(value).trim();
  return text || null;
};

const parseOptionalInt = (value, field = "value") => {
  if (value === undefined || value === null || value === "") {
    return null;
  }
  const parsed = Number.parseInt(String(value), 10);
  if (!Number.isFinite(parsed)) {
    throw new Error(`Invalid integer for ${field}: ${value}`);
  }
  return parsed;
};

const buildFallbackId = ({
  type,
  artist,
  album,
  title,
  track_number,
  duration_ms,
  label,
}) => {
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
  return dedupeBlockedObjects(normalized);
};

export const dedupeBlockedObjects = (items) => {
  const deduped = new Map();
  items.forEach((item) => {
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

const buildStaticSourceDescription = (env = process.env) => {
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

export const describeBlockedSource = (env = process.env) => {
  const liveSource = normalizeString(env.BRIDGE_BLOCKED_SOURCE);
  if (liveSource === "roon_browse") {
    const specs = parseBrowseSpecs(env);
    return {
      configured: specs.length > 0,
      mode: "roon_browse_live",
      specs_count: specs.length,
    };
  }
  return buildStaticSourceDescription(env);
};

export const buildBlockedSnapshot = (env = process.env, now = new Date()) => {
  const items = loadBlockedObjects(env);
  if (items === null) {
    return null;
  }

  const objectTypes = Array.from(new Set(items.map((item) => item.type))).sort();
  return {
    contract_version: BLOCKED_CONTRACT_VERSION,
    generated_at: now.toISOString(),
    source: buildStaticSourceDescription(env),
    item_count: items.length,
    object_types: objectTypes,
    items,
  };
};

export const parseBrowseSpecs = (env = process.env) => {
  const raw = normalizeString(env.BRIDGE_BLOCKED_BROWSE_SPECS);
  if (!raw) {
    return [];
  }

  let parsed;
  try {
    parsed = JSON.parse(raw);
  } catch (error) {
    throw new Error(`BRIDGE_BLOCKED_BROWSE_SPECS is not valid JSON: ${error.message}`);
  }
  if (!Array.isArray(parsed)) {
    throw new Error("BRIDGE_BLOCKED_BROWSE_SPECS must be a JSON array");
  }
  return parsed.map((item, index) => normalizeBrowseSpec(item, index));
};

export const normalizeBrowseSpec = (payload, index = 0) => {
  if (!payload || typeof payload !== "object") {
    throw new Error(`Blocked browse spec ${index} must be an object`);
  }

  const object_type = normalizeString(payload.object_type)?.toLowerCase();
  if (!object_type || !SUPPORTED_TYPES.has(object_type)) {
    throw new Error(`Blocked browse spec ${index} has unsupported object_type`);
  }

  const path = Array.isArray(payload.path)
    ? payload.path.map((item) => normalizeString(item)).filter(Boolean)
    : [];
  if (!path.length) {
    throw new Error(`Blocked browse spec ${index} must include a non-empty path array`);
  }

  const subtitle_mapping = Array.isArray(payload.subtitle_mapping)
    ? payload.subtitle_mapping
        .map((item) => normalizeString(item))
        .filter((item) => item && ["artist", "album", "title"].includes(item))
    : [];

  return {
    name: normalizeString(payload.name) ?? `spec-${index + 1}`,
    hierarchy: normalizeString(payload.hierarchy) ?? "browse",
    path,
    object_type,
    subtitle_separator: normalizeString(payload.subtitle_separator) ?? " - ",
    subtitle_mapping,
  };
};

const browseAsync = (browseService, opts) =>
  new Promise((resolve, reject) => {
    browseService.browse(opts, (error, body) => {
      if (error) {
        reject(new Error(`Browse failed: ${error}`));
        return;
      }
      resolve(body);
    });
  });

const loadAsync = (browseService, opts) =>
  new Promise((resolve, reject) => {
    browseService.load(opts, (error, body) => {
      if (error) {
        reject(new Error(`Load failed: ${error}`));
        return;
      }
      resolve(body);
    });
  });

const findBrowseItem = (items, segment) => {
  const target = normalizeString(segment)?.toLowerCase();
  if (!target) {
    return null;
  }
  return (
    items.find((item) => normalizeString(item.title)?.toLowerCase() === target) ??
    items.find((item) => normalizeString(item.subtitle)?.toLowerCase() === target) ??
    null
  );
};

const loadAllItems = async (browseService, hierarchy, level, count) => {
  const items = [];
  let offset = 0;
  const pageSize = 100;
  const expectedCount = Math.max(0, count ?? 0);
  while (offset < expectedCount || (offset === 0 && expectedCount === 0)) {
    const response = await loadAsync(browseService, {
      hierarchy,
      level,
      offset,
      count: pageSize,
    });
    const pageItems = Array.isArray(response.items) ? response.items : [];
    items.push(...pageItems);
    if (!pageItems.length || pageItems.length < pageSize) {
      break;
    }
    offset += pageItems.length;
  }
  return items;
};

const browseToPath = async (browseService, spec) => {
  let current = await browseAsync(browseService, {
    hierarchy: spec.hierarchy,
    pop_all: true,
  });

  for (const segment of spec.path) {
    if (current.action !== "list" || !current.list) {
      throw new Error(`Browse path step "${segment}" did not return a list`);
    }
    const items = await loadAllItems(
      browseService,
      spec.hierarchy,
      current.list.level,
      current.list.count,
    );
    const matched = findBrowseItem(items, segment);
    if (!matched || !matched.item_key) {
      throw new Error(`Could not resolve browse path segment "${segment}"`);
    }
    current = await browseAsync(browseService, {
      hierarchy: spec.hierarchy,
      item_key: matched.item_key,
    });
  }

  if (current.action !== "list" || !current.list) {
    throw new Error(`Final browse result for spec "${spec.name}" did not return a list`);
  }
  return loadAllItems(browseService, spec.hierarchy, current.list.level, current.list.count);
};

export const blockedObjectFromBrowseItem = (item, spec) => {
  const title = normalizeString(item?.title);
  const subtitle = normalizeString(item?.subtitle);
  const payload = { type: spec.object_type };

  if (spec.object_type === "artist") {
    payload.artist = title;
  } else if (spec.object_type === "album") {
    payload.album = title;
    if (subtitle) {
      payload.artist = subtitle;
    }
  } else if (spec.object_type === "track") {
    payload.title = title;
    if (subtitle && spec.subtitle_mapping.length) {
      const parts = subtitle.split(spec.subtitle_separator).map((part) => normalizeString(part));
      spec.subtitle_mapping.forEach((field, index) => {
        if (parts[index]) {
          payload[field] = parts[index];
        }
      });
    } else if (subtitle) {
      payload.artist = subtitle;
    }
  }

  payload.label = buildFallbackLabel({
    type: spec.object_type,
    artist: payload.artist,
    album: payload.album,
    title: payload.title,
    label: title,
  });
  return normalizeBlockedObject(payload);
};

export const collectBlockedFromBrowseSpecs = async (browseService, specs) => {
  const blocked = [];
  for (const spec of specs) {
    const items = await browseToPath(browseService, spec);
    items
      .filter((item) => item && item.hint !== "header")
      .forEach((item) => blocked.push(blockedObjectFromBrowseItem(item, spec)));
  }
  return dedupeBlockedObjects(blocked);
};

const toIso = (value) => (value instanceof Date ? value.toISOString() : null);

export const createBrowseBlockedProvider = (
  env = process.env,
  { getBrowseService, now = () => new Date() } = {},
) => {
  const specs = parseBrowseSpecs(env);
  const cacheTtlSeconds = parseOptionalInt(
    env.BRIDGE_BLOCKED_CACHE_SECONDS,
    "BRIDGE_BLOCKED_CACHE_SECONDS",
  ) ?? DEFAULT_CACHE_SECONDS;
  const state = {
    snapshot: null,
    lastAttemptAt: null,
    lastSuccessAt: null,
    lastError: null,
    inFlight: null,
  };

  const refresh = async ({ force = false } = {}) => {
    const currentTime = now();
    if (!force && state.snapshot && state.lastSuccessAt) {
      const ageMs = currentTime.getTime() - state.lastSuccessAt.getTime();
      if (ageMs < cacheTtlSeconds * 1000) {
        return state.snapshot;
      }
    }
    if (state.inFlight) {
      return state.inFlight;
    }

    state.lastAttemptAt = currentTime;
    state.inFlight = (async () => {
      try {
        const browseService = getBrowseService?.();
        if (!browseService) {
          throw new Error("Roon browse service is not available");
        }
        const items = await collectBlockedFromBrowseSpecs(browseService, specs);
        const snapshot = {
          contract_version: BLOCKED_CONTRACT_VERSION,
          generated_at: currentTime.toISOString(),
          source: {
            configured: specs.length > 0,
            mode: "roon_browse_live",
            specs_count: specs.length,
          },
          item_count: items.length,
          object_types: Array.from(new Set(items.map((item) => item.type))).sort(),
          items,
        };
        state.snapshot = snapshot;
        state.lastSuccessAt = currentTime;
        state.lastError = null;
        return snapshot;
      } catch (error) {
        state.lastError = error.message;
        throw error;
      } finally {
        state.inFlight = null;
      }
    })();

    return state.inFlight;
  };

  return {
    async getSnapshot() {
      return refresh();
    },
    refresh,
    describe() {
      return {
        configured: specs.length > 0,
        mode: "roon_browse_live",
        specs_count: specs.length,
        cache_ttl_seconds: cacheTtlSeconds,
        last_attempt_at: toIso(state.lastAttemptAt),
        last_success_at: toIso(state.lastSuccessAt),
        last_error: state.lastError,
        item_count: state.snapshot?.item_count ?? 0,
        live_connected: Boolean(getBrowseService?.()),
      };
    },
  };
};

export const createBlockedProvider = (
  env = process.env,
  deps = {},
) => {
  const liveSource = normalizeString(env.BRIDGE_BLOCKED_SOURCE);
  if (liveSource === "roon_browse") {
    return createBrowseBlockedProvider(env, deps);
  }

  return {
    async getSnapshot() {
      return buildBlockedSnapshot(env);
    },
    async refresh() {
      return buildBlockedSnapshot(env);
    },
    describe() {
      const snapshot = buildBlockedSnapshot(env);
      if (snapshot === null) {
        return {
          ...buildStaticSourceDescription(env),
          contract_version: null,
          generated_at: null,
          item_count: 0,
          object_types: [],
        };
      }
      return {
        ...snapshot.source,
        contract_version: snapshot.contract_version,
        generated_at: snapshot.generated_at,
        item_count: snapshot.item_count,
        object_types: snapshot.object_types,
      };
    },
  };
};
