# Design vs Implementation Matrix for HSAJ

## Scope

This document compares the target design in `DESIGN.md` with the current runtime implementation in:

- `bridge/src/`
- `core/src/hsaj/`
- `configs/hsaj.example.yaml`

`DESIGN.md` is treated as the intended product direction. Runtime code is treated as the source of truth for what actually works today.

## Summary

The project now implements more than the original MVP:

- library scan into SQLite, including a normalized artist/album/track graph
- bridge transport ingestion into `play_history`
- Atmos detection and relocation
- blocked sync with first-seen timing, restore semantics, and runtime status tracking
- quarantine planning, apply, cleanup retention, and restore
- advisory soft candidates for duplicates, stale inbox items, and never-played-old files
- operator HTTP API/UI plus background jobs
- dependency-aware probes, destructive guardrails, and clean-room bootstrap/smoke tooling

The main remaining gaps to full draft parity are now narrower:

- blocked state still depends on bridge extraction rather than a first-class Roon blocked API
- Roon favorites/tags are not yet imported as protection signals
- smart cleanup remains conservative and advisory outside explicit blocked flows
- live Roon browse validation still needs the final manual pre-release run against a real environment

## Matrix

| Design area | Draft expectation | Current implementation | Status | Notes |
| --- | --- | --- | --- | --- |
| Core product idea | Scan library, listen to Roon, protect Atmos, quarantine unwanted files, keep truth in FS + local DB | Implemented as scanner, transport ingestion, blocked sync, Atmos planning, quarantine apply, restore, cleanup retention, and operator review flows | Partial | Core flows exist; remaining work is mostly around richer external signals and hardening |
| Component split | Separate scanner, Roon integration layer, core policy engine, action executor, CLI, future web UI | Split into `bridge/` and `core/`; core is modular across scanner, blocking, planner, executor, server, runtime jobs, DB, CLI | Implemented | The current built-in operator UI covers the draft's future API/UI direction for now |
| Source of truth | Filesystem for file facts, Roon for user intent, SQLite for normalized state | `files`, `library_artists`, `library_albums`, `library_tracks`, `play_history`, `roon_blocks_raw`, `block_candidates`, `actions_log`, `plan_runs`, `review_decisions` exist in SQLite | Implemented | The normalized library graph is now real, not just implied |
| File scanning | Read tags, format, duration, Atmos state, store in SQLite | Scanner reads tags via `mutagen`, duration via `mutagen`, Atmos via `ffprobe`, and syncs normalized graph rows | Implemented | This area is materially aligned with the design |
| Roon integration | Bridge should expose blocked state and playback behavior to the core | Bridge exposes `/live`, `/health`, `/ready`, `/metrics`, `/track/{id}`, `/blocked`, and WS `/events`; core persists blocked sync status and validates blocked contract/source mode | Partial | Live blocked mode is implemented via browse extraction and still depends on Roon browse path configuration |
| Block inheritance | Track, album, and artist blocks should cascade with `track > album > artist` priority | Planner resolves track, album, and artist candidates using cached metadata and normalized graph rows, with explicit priority ordering | Implemented | The current graph-backed matching is much closer to the draft than earlier matrix versions |
| First-seen timer | Grace timer starts when HSAJ first sees a block | `first_seen_at`, `last_seen_at`, `planned_action_at`, and restore transitions are persisted and preserved | Implemented | This matches the draft well |
| Restore flow | Removing a block should cancel action; quarantined files should be reversible | Candidates are marked `restored` when blocks disappear; quarantine restore is implemented with conflict handling and audit logs | Implemented | Reversibility remains one of the strongest parts of the system |
| Pre-action validation | Re-check blocked state, Atmos immunity, whitelist/favorites, then quarantine | Stored previews are validated before apply for stale paths, candidate state, Atmos immunity, exemptions, destination conflicts, blocked contract/source-mode mismatches, and stale blocked sync | Partial | Manual exemptions exist; favorites/tags from Roon are still missing |
| Atmos policy | Detect Atmos with `ffprobe`, move to dedicated root, never auto-delete | Implemented in `atmos.py`, `planner.py`, and `executor.py` | Implemented | Current code stores `atmos_detected` and protects files already under `atmos_dir` |
| Behavior scoring | Build soft candidates from history, age, likes, inbox age, duplicates | Planner builds advisory soft candidates for duplicate quality, stale inbox items, and never-played-old files; operator review can dismiss, exempt, preview, and apply | Partial | Likes/tags import is still missing, and soft candidates remain advisory by design |
| CLI surface | `scan`, `sync-roon`, `plan`, `apply`, `history` | `scan`, `plan`, `apply`, `history`, `listen`, `restore`, `cleanup`, `serve`, `db *`, `roon sync`, `exempt *` | Implemented differently | The CLI is richer than the draft |
| Core API / UI | Future `GET /plan`, `POST /apply`, `GET /stats`, `GET /candidates` | Operator API and built-in UI expose plan preview/validate/apply, stats, candidates, soft review, reviews, runtime jobs, actions, exemptions, cleanup, and restore | Implemented | This is now present in runtime code |
| Config | Paths, grace days, quarantine delete days, auto-delete, behavior scoring | YAML config includes database, paths, policy, bridge, security, observability, and runtime sections | Implemented | Guardrails now include `policy.allow_hard_delete`, `bridge.required_source_mode`, and `bridge.max_blocked_sync_age_minutes` |
| Hard delete | Optional delete after quarantine retention period | `cleanup_retention` now marks expired candidates or physically deletes files when `auto_delete=true` and `allow_hard_delete=true` | Implemented | Delete logs now carry explicit audit details and cleanup remains idempotent when paths are already gone |
| Ops readiness | Health/ready should reflect dependencies, not only process liveness | Bridge/core now expose dependency-aware `/health`, `/ready`, `/metrics`, plus a clean-room bootstrap smoke path | Implemented | `hsaj serve` is the canonical production story; timer-based maintenance is legacy/dev-only |
| Repo structure | Split `bridge`, `core`, `configs`, ADRs, tests, scripts | Top-level structure is present; core is modular; bridge remains concentrated in `bridge/src/index.js` plus `blocked.js` | Partial | Functional split is solid, but bridge decomposition can still improve |

## Most Important Remaining Gaps

### 1. Live blocked integration is real, but still constrained by Roon browse extraction

The bridge can now expose a live blocked snapshot from configured Roon browse paths, but this is still a derived feed rather than a dedicated upstream blocked API.

### 2. Protection signals are still incomplete

Manual exemptions exist and cover part of the draft's whitelist story, but favorites/tags from Roon are not yet imported into the core as automatic immunity signals.

### 3. Smart cleanup is intentionally conservative

The system already generates advisory soft candidates and lets operators review them, but it does not auto-delete or auto-quarantine anything outside the explicit blocked flow. That is safer, but still short of the draft's longer-term ambition for richer decision support.

### 4. Production readiness now hinges on live-environment validation

Reproducible bootstrap, dependency-aware readiness, and destructive-operation guardrails now exist in runtime code and docs. The biggest remaining release risk is the final live browse validation against an actual Roon environment, especially around browse-path stability and the manual end-to-end release rehearsal.

## Bottom Line

The implementation is no longer an MVP-only skeleton. It already covers most of the design's core mechanics and now includes the operator API/UI, runtime jobs, advisory cleanup review, and quarantine retention execution.

For full production parity with `DESIGN.md`, the next meaningful steps are not "invent the system" but:

1. complete the live Roon browse release rehearsal
2. add richer protection signals from Roon
3. keep smart cleanup conservative until those external signals are available
