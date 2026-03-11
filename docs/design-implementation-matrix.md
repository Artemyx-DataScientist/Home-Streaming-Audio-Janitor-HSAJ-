# Design vs Implementation Matrix for HSAJ

## Scope

This document compares the target design in `DESIGN.md` with the current runtime implementation in:

- `bridge/src/`
- `core/src/hsaj/`
- `configs/hsaj.example.yaml`

`DESIGN.md` is treated as the intended product direction. Runtime code is treated as the source of truth for what actually works today.

## Summary

The project already implements a usable MVP around four core flows:

- library scan into SQLite
- bridge transport events into `play_history`
- Atmos detection and relocation
- quarantine planning, apply, and restore

The largest gaps are still in the "smart cleanup" part of the draft:

- blocked flow depends on an external `/blocked` feed instead of a real Roon blocked API
- artist and album blocks are resolved by direct metadata matching, not by a richer library object graph
- behavior scoring and duplicate quality logic are still not implemented
- there is no core HTTP API or web UI
- quarantine hard delete is configured, but not executed anywhere

## Matrix

| Design area | Draft expectation | Current implementation | Status | Notes |
| --- | --- | --- | --- | --- |
| Core product idea | Scan library, listen to Roon, protect Atmos, quarantine unwanted files, keep truth in FS + local DB | Implemented as `scan`, bridge WS transport events, Atmos moves, quarantine planning/apply, restore, SQLite models | Partial | MVP exists, but "trash detection", duplicate logic, and complete blocked integration are still incomplete |
| Component split | Separate scanner, Roon integration layer, core policy engine, action executor, CLI, future web UI | Split into `bridge/` and `core/`; core is further separated into scanner, blocking, planner, executor, transport, DB, CLI | Partial | Architectural direction matches the draft; no core HTTP API or web UI yet |
| Source of truth | Filesystem for file facts, Roon for user intent, SQLite for normalized state | `files`, `play_history`, `actions_log`, `roon_blocks_raw`, `block_candidates`, `roon_items_cache` exist in SQLite | Partial | Local DB is real, but there is still no `library_items` abstraction for artist/album/track entities |
| File scanning | Read tags, format, duration, Atmos state, store in SQLite | Scanner reads tags via `mutagen`, duration via `mutagen`, Atmos via `ffprobe`, stores `atmos_detected` in `files` | Implemented | The DB model now stores Atmos state directly, which is slightly ahead of the original matrix doc |
| Roon integration | Bridge should expose blocked state and playback behavior to the core | Bridge exposes `/health`, `/track/{id}`, `/blocked`, and WS `/events` | Partial | Transport flow works. `/blocked` works only when `BRIDGE_BLOCKED_JSON` or `BRIDGE_BLOCKED_FILE` is configured |
| Block inheritance | Track, album, and artist blocks should cascade with `track > album > artist` priority | Planner sorts by `track`, `album`, `artist` priority and resolves each candidate into file matches | Partial | There is no richer inheritance graph; album and artist handling is based on exact metadata matches in `files` |
| First-seen timer | Grace timer starts when HSAJ first sees a block | `first_seen_at`, `last_seen_at`, and `planned_action_at` are persisted and preserved on later syncs | Implemented | This part matches the design well |
| Restore flow | Removing a block should cancel action; quarantined files should be reversible | Candidates are marked `restored` when a block disappears; quarantine restore is implemented with audit logs | Implemented | Reversibility is already one of the strongest parts of the system |
| Pre-action validation | Re-check blocked state, Atmos immunity, whitelist/favorites, then quarantine | Planner prevents Atmos items from being quarantined; executor checks file existence and current status | Partial | No whitelist/favorites. No second blocked-state revalidation at apply time |
| Atmos policy | Detect Atmos with `ffprobe`, move to dedicated root, never auto-delete | Implemented in `atmos.py`, planned in `planner.py`, executed in `executor.py` | Implemented | Current code stores `atmos_detected` and also protects files already under `atmos_dir` |
| Behavior scoring | Build soft candidates from history, age, likes, inbox age, duplicates | `play_history` exists, but planner does not use it for scoring | Not implemented | `enable_behavior_scoring` exists in config as a placeholder only |
| CLI surface | `scan`, `sync-roon`, `plan`, `apply`, `history` | `scan`, `plan`, `apply`, `history`, `listen`, `restore`, `db init`, `db status`, `roon sync` | Implemented differently | The CLI is richer than the draft, but naming changed to `hsaj roon sync` |
| Core API / UI | Future `GET /plan`, `POST /apply`, `GET /stats`, `GET /candidates` | No core HTTP API and no web UI | Not implemented | Only the bridge exposes HTTP and WebSocket endpoints |
| Config | Paths, grace days, quarantine delete days, auto-delete, behavior scoring | YAML config now includes `database`, `paths`, and `policy` sections | Implemented | Config support is ahead of the previous comparison doc; policy settings are already modeled |
| Hard delete | Optional delete after quarantine retention period | `quarantine_delete_days` and `auto_delete` exist in config, but executor never performs timed deletion | Not implemented | Config is there, behavior is not |
| Repo structure | Split `bridge`, `core`, `configs`, ADRs, tests, scripts | Top-level structure is present; core is modular; bridge is still mostly concentrated in `bridge/src/index.js` | Partial | Repository shape follows the draft, but bridge has not yet been decomposed into smaller modules |

## What Matches Well Today

- `core/src/hsaj/scanner.py` scans the library and upserts file metadata into SQLite.
- `core/src/hsaj/transport.py` consumes bridge transport events and writes `play_history`.
- `core/src/hsaj/atmos.py` detects Atmos via `ffprobe` metadata and plans moves into `atmos_dir`.
- `core/src/hsaj/planner.py` builds both Atmos and quarantine plans.
- `core/src/hsaj/executor.py` applies Atmos and quarantine moves and supports restore from quarantine.
- `core/src/hsaj/config.py` now models both path settings and policy settings from the draft.

## Most Important Gaps

### 1. Blocked flow is still only partially real

The draft assumes a reliable Roon-driven blocked signal. In practice, the bridge only serves `/blocked` from:

- `BRIDGE_BLOCKED_JSON`
- `BRIDGE_BLOCKED_FILE`

That makes blocked sync operational for demos and controlled environments, but not fully coupled to live Roon block state yet.

### 2. Album and artist blocks are heuristic, not model-driven

The design suggests a richer library model where artist and album blocks expand naturally into concrete files. Current planner behavior is simpler:

- track blocks use Roon metadata or cached track metadata
- album blocks match `File.album` and optionally `File.artist`
- artist blocks match `File.artist`

This works for straightforward metadata, but it is not the same as a dedicated `library_items` graph with explicit inheritance semantics.

### 3. Smart cleanup logic is still mostly future work

The draft spends significant effort on soft scoring and duplicate handling. Current runtime code does not yet:

- derive `soft_candidates`
- rank by never-played history
- compare duplicate quality
- use likes, tags, or inbox age

So the implemented system is stronger at safe movement and auditability than at automatic decision-making.

### 4. Quarantine retention policy is modeled, but not executed

The config already includes:

- `policy.quarantine_delete_days`
- `policy.auto_delete`

But there is no worker or executor path that deletes files after the retention period. The system currently stops at quarantine plus manual restore.

## Bottom Line

The implementation is no longer just a skeleton. It already delivers a practical MVP for scanning, Atmos handling, transport history, quarantine planning, applying moves, and restoring mistakes.

The codebase still falls short of the full product promise in `DESIGN.md` mostly in three places:

1. real end-to-end blocked integration
2. smarter candidate generation beyond explicit blocks
3. automation after quarantine retention
