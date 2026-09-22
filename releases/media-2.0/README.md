# RELAX47 media-2.0

Server-owned video preparation for saved stays, 22 September 2026.

- A saved new stay schedules preparation after two hours; reconciliation survives a closed browser, missed event and HA restart.
- SQL job identity includes the stay ID and only media-relevant inputs. Notes, financial fields and lifecycle/version-only updates do not invalidate videos.
- Changed media inputs after a completed build require explicit manual preparation. Repeated clicks are idempotent; stale work cannot publish over newer inputs.
- Three separate parts (house, smart home, rules); full course is validated FFmpeg stream-copy concatenation. Approved existing artwork and core narration are retained. Dates are absolute so future preparation remains valid.
- UI obtains media per stay through authenticated HA services. Guest requests are restricted to their active stay and TV permission. No shared last-render pointer selects playback.
- Old files and the legacy render queue are retained. Matching legacy full/rules remain available while the first new bundle is prepared. Cancelled/completed and unclassified historical stays do not auto-render.
- Versioned auxiliary SQL tables are added to the existing private render queue; the main relational database schema 12 is unchanged.

## Package

`deployment.json` contains exact replacement operations with precondition hashes. All operations must pass readback and HA configuration validation; restart only after staging the backend. Redacted source regions are excluded from patches. No credentials or production guest rows are included.

`changes/queue.py` is the complete new queue/server pipeline. `changes/voice.js` is the updated browser narration section selector. Other changes are reviewable in deployment.json; do not replace full production files with redacted exports.

## Verification

From this folder:

```
python3 test_pipeline.py
python3 test_services.py
python3 test_concat.py
node test_client.cjs
```

15 real SQLite queue tests; two asynchronous service/worker tests with HA/TTS doubles and real FFmpeg; direct three-part concatenation; browser-state isolation/idempotency tests. Modified Python/JavaScript syntax checked locally with placeholders only in untouched redacted regions. Production checks must additionally confirm HA startup, the new services, scheduler persistence and one actual synthesis/render. Physical TV playback is a separate on-object check and must not be inferred from MP4 validity.

## Rollback

Stop new media workers (stop/restart HA as part of the controlled rollback), reverse exact code changes against verified deployed file hashes, and restart. Retain the private queue DB and outputs; the old queue ignores auxiliary tables. Never overwrite the business database or delete guest videos during code rollback.
