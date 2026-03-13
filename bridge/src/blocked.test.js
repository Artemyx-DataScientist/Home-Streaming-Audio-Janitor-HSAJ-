import test from "node:test";
import assert from "node:assert/strict";
import os from "node:os";
import path from "node:path";
import fs from "node:fs";

import {
  BLOCKED_CONTRACT_VERSION,
  blockedObjectFromBrowseItem,
  buildBlockedSnapshot,
  collectBlockedFromBrowseSpecs,
  createBlockedProvider,
  describeBlockedSource,
  loadBlockedObjects,
  normalizeBlockedObject,
  normalizeBrowseSpec,
  parseBrowseSpecs,
} from "./blocked.js";

test("normalizeBlockedObject preserves metadata and builds fallback ids", () => {
  const normalized = normalizeBlockedObject({
    type: "album",
    artist: "Artist",
    album: "Album",
  });

  assert.equal(normalized.type, "album");
  assert.equal(normalized.artist, "Artist");
  assert.equal(normalized.album, "Album");
  assert.equal(normalized.label, "Artist - Album");
  assert.match(normalized.id, /^album:/);
});

test("loadBlockedObjects reads and deduplicates blocked entries from file", () => {
  const tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), "hsaj-bridge-"));
  const filePath = path.join(tmpDir, "blocked.json");
  fs.writeFileSync(
    filePath,
    JSON.stringify([
      { type: "artist", id: "artist-1", artist: "Artist" },
      { type: "artist", id: "artist-1", artist: "Artist", label: "Artist" },
      { type: "track", title: "Song", artist: "Artist", duration_ms: 123000 },
    ]),
    "utf8",
  );

  const blocked = loadBlockedObjects({ BRIDGE_BLOCKED_FILE: filePath });

  assert.equal(blocked.length, 2);
  assert.deepEqual(blocked[0], {
    type: "artist",
    id: "artist-1",
    label: "Artist",
    artist: "Artist",
    album: null,
    title: null,
    track_number: null,
    duration_ms: null,
  });
  assert.equal(blocked[1].type, "track");
  assert.equal(blocked[1].title, "Song");

  fs.rmSync(tmpDir, { recursive: true, force: true });
});

test("describeBlockedSource reports configured static and live modes", () => {
  assert.deepEqual(describeBlockedSource({ BRIDGE_BLOCKED_JSON: "[]" }), {
    configured: true,
    mode: "inline_json",
  });
  assert.deepEqual(describeBlockedSource({ BRIDGE_BLOCKED_FILE: "/tmp/blocked.json" }), {
    configured: true,
    mode: "file",
    file_path: "/tmp/blocked.json",
  });
  assert.deepEqual(
    describeBlockedSource({
      BRIDGE_BLOCKED_SOURCE: "roon_browse",
      BRIDGE_BLOCKED_BROWSE_SPECS: JSON.stringify([
        { path: ["Hidden Albums"], object_type: "album" },
      ]),
    }),
    {
      configured: true,
      mode: "roon_browse_live",
      specs_count: 1,
    },
  );
  assert.deepEqual(describeBlockedSource({}), {
    configured: false,
    mode: "unconfigured",
  });
});

test("buildBlockedSnapshot returns versioned envelope with metadata", () => {
  const snapshot = buildBlockedSnapshot(
    {
      BRIDGE_BLOCKED_JSON: JSON.stringify([
        { type: "artist", id: "artist-1", artist: "Artist" },
        { type: "album", artist: "Artist", album: "Album" },
      ]),
    },
    new Date("2024-01-02T03:04:05.000Z"),
  );

  assert.equal(snapshot.contract_version, BLOCKED_CONTRACT_VERSION);
  assert.equal(snapshot.generated_at, "2024-01-02T03:04:05.000Z");
  assert.equal(snapshot.item_count, 2);
  assert.deepEqual(snapshot.object_types, ["album", "artist"]);
  assert.equal(snapshot.source.mode, "inline_json");
  assert.equal(snapshot.items[0].type, "artist");
});

test("parseBrowseSpecs validates live browse configuration", () => {
  const specs = parseBrowseSpecs({
    BRIDGE_BLOCKED_BROWSE_SPECS: JSON.stringify([
      {
        name: "hidden-albums",
        hierarchy: "albums",
        path: ["Hidden Albums"],
        object_type: "album",
      },
    ]),
  });

  assert.deepEqual(specs, [
    {
      name: "hidden-albums",
      hierarchy: "albums",
      path: ["Hidden Albums"],
      object_type: "album",
      subtitle_separator: " - ",
      subtitle_mapping: [],
    },
  ]);
});

test("blockedObjectFromBrowseItem maps track subtitle fields", () => {
  const spec = normalizeBrowseSpec({
    path: ["Hidden Tracks"],
    object_type: "track",
    subtitle_mapping: ["artist", "album"],
  });
  const blocked = blockedObjectFromBrowseItem(
    {
      title: "Track Title",
      subtitle: "Artist Name - Album Name",
    },
    spec,
  );

  assert.equal(blocked.type, "track");
  assert.equal(blocked.title, "Track Title");
  assert.equal(blocked.artist, "Artist Name");
  assert.equal(blocked.album, "Album Name");
});

test("collectBlockedFromBrowseSpecs traverses live browse tree", async () => {
  const rootItems = [{ title: "Hidden Albums", item_key: "hidden-albums", hint: "list" }];
  const hiddenAlbumItems = [{ title: "Album One", subtitle: "Artist One", hint: "list" }];
  const browseCalls = [];
  const loadCalls = [];

  const browseService = {
    browse(opts, cb) {
      browseCalls.push(opts);
      if (opts.pop_all) {
        cb(false, {
          action: "list",
          list: { level: 0, count: rootItems.length },
        });
        return;
      }
      if (opts.item_key === "hidden-albums") {
        cb(false, {
          action: "list",
          list: { level: 1, count: hiddenAlbumItems.length },
        });
        return;
      }
      cb("UnexpectedBrowse", {});
    },
    load(opts, cb) {
      loadCalls.push(opts);
      if (opts.level === 0) {
        cb(false, { items: rootItems, list: { level: 0, count: rootItems.length }, offset: 0 });
        return;
      }
      if (opts.level === 1) {
        cb(false, {
          items: hiddenAlbumItems,
          list: { level: 1, count: hiddenAlbumItems.length },
          offset: 0,
        });
        return;
      }
      cb("UnexpectedLoad", {});
    },
  };

  const blocked = await collectBlockedFromBrowseSpecs(browseService, [
    normalizeBrowseSpec({
      hierarchy: "albums",
      path: ["Hidden Albums"],
      object_type: "album",
    }),
  ]);

  assert.equal(blocked.length, 1);
  assert.equal(blocked[0].type, "album");
  assert.equal(blocked[0].album, "Album One");
  assert.equal(blocked[0].artist, "Artist One");
  assert.equal(browseCalls.length, 2);
  assert.equal(loadCalls.length, 2);
});

test("createBlockedProvider returns live browse snapshot when configured", async () => {
  const browseService = {
    browse(opts, cb) {
      if (opts.pop_all) {
        cb(false, { action: "list", list: { level: 0, count: 1 } });
        return;
      }
      cb(false, { action: "list", list: { level: 1, count: 1 } });
    },
    load(opts, cb) {
      if (opts.level === 0) {
        cb(false, {
          items: [{ title: "Hidden Artists", item_key: "hidden-artists", hint: "list" }],
          list: { level: 0, count: 1 },
          offset: 0,
        });
        return;
      }
      cb(false, {
        items: [{ title: "Artist One", hint: "list" }],
        list: { level: 1, count: 1 },
        offset: 0,
      });
    },
  };

  const provider = createBlockedProvider(
    {
      BRIDGE_BLOCKED_SOURCE: "roon_browse",
      BRIDGE_BLOCKED_BROWSE_SPECS: JSON.stringify([
        { path: ["Hidden Artists"], object_type: "artist" },
      ]),
      BRIDGE_BLOCKED_CACHE_SECONDS: "600",
    },
    {
      getBrowseService: () => browseService,
    },
  );

  const snapshot = await provider.getSnapshot();
  const description = provider.describe();

  assert.equal(snapshot.contract_version, "v2");
  assert.equal(snapshot.item_count, 1);
  assert.equal(snapshot.items[0].artist, "Artist One");
  assert.equal(description.mode, "roon_browse_live");
  assert.equal(description.live_connected, true);
  assert.equal(description.item_count, 1);
});


test("createBlockedProvider reports browse errors, serves cache, and recovers", async () => {
  let browseService = {
    browse(opts, cb) {
      if (opts.pop_all) {
        cb(false, { action: "list", list: { level: 0, count: 1 } });
        return;
      }
      cb(false, { action: "list", list: { level: 1, count: 1 } });
    },
    load(opts, cb) {
      if (opts.level === 0) {
        cb(false, {
          items: [{ title: "Hidden Artists", item_key: "hidden-artists", hint: "list" }],
          list: { level: 0, count: 1 },
          offset: 0,
        });
        return;
      }
      cb(false, {
        items: [{ title: "Recovered Artist", hint: "list" }],
        list: { level: 1, count: 1 },
        offset: 0,
      });
    },
  };
  const provider = createBlockedProvider(
    {
      BRIDGE_BLOCKED_SOURCE: "roon_browse",
      BRIDGE_BLOCKED_BROWSE_SPECS: JSON.stringify([
        { path: ["Hidden Artists"], object_type: "artist" },
      ]),
      BRIDGE_BLOCKED_CACHE_SECONDS: "600",
    },
    {
      getBrowseService: () => browseService,
    },
  );

  const recovered = await provider.refresh({ force: true });
  assert.equal(recovered.item_count, 1);
  assert.equal(provider.describe().last_error, null);
  assert.equal(provider.describe().item_count, 1);

  browseService = null;
  const cached = await provider.getSnapshot();
  assert.equal(cached.item_count, 1);
  assert.equal(cached.items[0].artist, "Recovered Artist");
});
