# Personal LAN Assist edition

`VideoTranscoderLanAssist.exe` is a separate, opt-in companion to the generic
`VideoTranscoderPortable.exe`. The generic portable app keeps its normal GUI
and does not start a network service.

The LAN edition uses one external `VideoTranscoderLanAssist.json` beside the
EXE (or a file selected with `--config`). No hostname, media path, or bearer
token is embedded in the executable.

## Roles

- The storage PC runs `mode: "coordinator"`, converts the smallest remaining
  file with Intel QSV, validates all results, publishes final MKV files, and is
  the only PC allowed to delete an original.
- The helper PC runs `mode: "helper"`, converts the largest remaining file
  with NVIDIA NVENC, uploads an attempt-specific candidate, and never publishes
  a final media name or deletes a remote original.
- If the helper loses the LAN connection, its remote attempt is cancelled and
  fenced. It then completes one file from its configured local fallback folder,
  retains that original, and reconnects.

Control traffic is an authenticated loopback HTTP API carried through an SSH
local forward. The coordinator exposes each assigned source through an opaque,
attempt-scoped hard-link alias in its private staging folder. Media reads and
candidate uploads therefore use only the configured staging SMB path; the
helper never receives the original filename.

The coordinator media root and work root must be on the same local NTFS
volume so those no-copy aliases and final transactional renames remain atomic.

## Dashboard

On the storage PC, open `http://127.0.0.1:41802/` while the coordinator is
running. The loopback-only dashboard shows:

- the current filename and phase for both PCs;
- conversion progress, speed, and ETA;
- upload bytes, transfer rate, and transfer ETA;
- queue, completion/failure totals, and recent activity.

Filename details are not returned by the helper API or written to the aggregate
status file.

## Resume and failure behavior

Completed output identities are recorded in a durable ledger and skipped after
a restart. An interrupted encode restarts that file from the beginning; FFmpeg
hardware HEVC output cannot safely resume in the middle of a file. Transaction
journals are recovered by verified rollback or roll-forward. Ambiguous identity
or hash state remains fail-closed.

An isolated media failure leaves that source untouched and advances the queue.
The coordinator stops after three consecutive terminal failures.

## Build

```powershell
powershell -NoProfile -File .\scripts\build_lan_assist.ps1
```

The result is `dist\lan-assist\VideoTranscoderLanAssist.exe`. Start from the
coordinator or helper example JSON in `packaging\portable`, then create one
shared token without printing it:

```powershell
.\VideoTranscoderLanAssist.exe --config .\VideoTranscoderLanAssist.json --create-token
```

Copy that token file securely to the other PC and reference the local copy from
each configuration. Token creation is allowed only on local storage. On
Windows, the app removes ACL inheritance, grants full control only to the
current user, verifies that exact protected DACL, and deletes the new token if
hardening or verification fails. UNC paths and mapped network drives are
rejected; copy the token into an owner-only local path on each PC rather than
using one token directly from a share. OpenSSH key or agent authentication must
already work in batch mode.
