import test from "node:test";
import assert from "node:assert/strict";
import os from "node:os";
import path from "node:path";
import fs from "node:fs";

import { loadBlockedObjects, normalizeBlockedObject } from "./blocked.js";

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
