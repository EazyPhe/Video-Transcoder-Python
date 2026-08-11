# HOT-BOX cutover report - 2026-08-11

This report records the accepted HOT-BOX deployment and clean-run evidence. It
is a historical result, not a source of live worker status.

## Objective and usage

The goal was to diagnose recurring HOT-BOX transcoder failures, make HOT-BOX
the primary headless NVENC worker, retain INSPIRON as coordinator and publisher,
and complete a clean run without source or output loss.

| Metric | Recorded value |
| --- | ---: |
| Codex task tokens | 2,179,444 |
| Elapsed time | 15,878 seconds (4h 24m 38s) |

The token count above is Codex task usage recorded when the persistent goal was
closed. It is not an API billing total or the LAN Assist bearer token, and no
credential value is recorded here.

## Accepted result

- HOT-BOX completed the verified job with `completed=1` and `failed=0`, then
  automatically accepted the next job while INSPIRON remained idle.
- The published output was 5,667,881,105 bytes in Matroska format with HEVC
  video, AAC audio, and the full 3,186.666-second duration.
- The original source was removed only after publication and final validation.
- Ledger and output identity remained stable across two terminal checks.
- The full test run reported 562 passed, 2 skipped, and 1 deselected.
- The short-lived transaction-journal phases were not individually captured.
  Direct evidence covered the pre-commit state, `PostPublishIntegrity`, the
  terminal state, and stable post-run identity checks.

## Implementation commits

- `f743afa` - Harden coordinator, helper, validation, dashboard, control, and
  NVENC behavior.
- `f653bea` - Add fail-closed Windows launch and deployment operations.
