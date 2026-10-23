# Changes

## 1.1.0 — configurable sources

- Removed hardcoded Telegram channel usernames from application source code.
- Added `channels` list to Home Assistant Configuration tab; default empty.
- Added configurable per-channel batch size, per-post character cap, briefing word guideline, and deduplication preference.
- Initial checkpoints are based on configured channels. Newly added channels start from latest while existing checkpoints remain intact.
- Updated Home Assistant voice script aliases to be topic-independent.
- Corrected documentation for repository-specific internal app hostnames.
- Added unit tests for validation, checkpointing, and source-list changes.
