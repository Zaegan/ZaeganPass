# ZaeganPass

A minimal, security-focused Android password generator.

## Features

- Cryptographically secure generation via `java.security.SecureRandom` (OS-seeded CSPRNG — never `java.util.Random`)
- Configurable character sets: lowercase, uppercase, numbers, symbols
- Option to exclude visually ambiguous characters (`0 O o 1 l I`)
- Adjustable length (8 – 128 characters)
- Guarantees at least one character from each enabled category; Fisher-Yates shuffle ensures no category is always in a predictable position
- Auto-copies to clipboard on generation; clipboard is automatically cleared after 60 seconds
- **The password is never written to disk, logged, or transmitted**
- User settings (length, toggles) persist across sessions via SharedPreferences
- `android:allowBackup="false"` — Android's auto-backup will not upload app data to Google Drive

## Building

### Via the build server (remote)

```bash
./buildserver.sh build ZaeganPass
```

Other commands:

```bash
./buildserver.sh submit <repo-url>     # queue a job, print job ID
./buildserver.sh status <job-id>       # one-shot status check
./buildserver.sh wait   <job-id>       # poll until done, then show result
./buildserver.sh log    <job-id>       # fetch full build log
```

### Via build_server.py (standalone / local)

`build_server.py` can be run as a self-contained build script without a running server. Pass `--repo-dir` to point it at a local checkout:

```bash
python3 build_server.py --repo-dir /path/to/ZaeganPass
```

Optional flags:

| Flag | Description |
|---|---|
| `--output-dir DIR` | Copy finished APK/AAB artifacts into `DIR` |
| `--clean` | Force a full scaffold rebuild (use after dependency changes) |

Output artifacts (when build succeeds):

- `app-release.apk` — unsigned release APK
- `app-releaseSigned.apk` — signed release APK (ready to install)
- `app-release.aab` — Android App Bundle

## Security notes

| Concern | Approach |
|---|---|
| RNG | `java.security.SecureRandom` (OS entropy pool) |
| Memory | Password generated as `char[]`, zeroed immediately after String conversion for display |
| Disk | Nothing written — no database, no files, no logs |
| Clipboard | Auto-cleared after 60 s; on API 28+ uses `clearPrimaryClip()`, older APIs overwrite with empty text |
| Backup | `android:allowBackup="false"` prevents Google Drive sync of app data |
| What persists | Only the user's *settings* (length, which character types, exclude-ambiguous toggle) |

## Project layout

```
build.json          ← build configuration (SDK versions, dependencies)
build_server.py     ← standalone build script / server
app/src/main/
  AndroidManifest.xml
  java/com/github/zaegan/zaeganpass/
    PasswordGenerator.java   ← pure generation logic, no Android dependencies
    MainActivity.java        ← UI, clipboard handling, settings persistence
  res/
    layout/activity_main.xml
    values/
    ...
```
