# vue-android-builder

Standalone Docker service for building Android APKs from Vue.js/Capacitor projects.

Both **Hermes Agent** and **OpenClaw** can dispatch builds to this service via HTTP.
The Android SDK is never needed in the agent containers.

## Architecture

```
Hermes / OpenClaw
      |
      | POST /build
      v
vue-android-builder (port 8083)
  - Android SDK (persistent volume)
  - Gradle cache (persistent volume)
  - Reads projects from /mount/vue
  - Uploads built APKs to TailDrive
  - Writes logs to /mount/logs
```

## API

### GET /health
Returns `{ status, sdk_ready, time }`.

### GET /projects
Lists all discoverable buildable projects.

### GET /status
Returns full build state: `{ running, job_id, queue, active, completed, failed, summary }`.

Active builds include live stage progress:
```json
{
  "project": "chronoquest",
  "stage": 4,
  "total_stages": 6,
  "stage_name": "Building Capacitor Android bundle",
  "elapsed_s": 142
}
```

### POST /build
Start a batch build.

```json
{ "projects": "all",   "parallel": 2 }   // build all projects, 2 at a time
{ "projects": ["chronoquest", "hss-v3"] } // build specific projects
{ "projects": "chronoquest" }             // build one project
```

Response:
```json
{ "started": true, "job_id": "a1b2c3d4", "message": "Build started. Poll GET /status every 30s for progress." }
```

### GET /logs/\<project\>
Last 200 lines of the most recent build log (plain text).

### GET /logs/\<project\>/json
Structured log response with all available log files listed.

---

## Project Convention

Projects are auto-discovered if they have:

**Option A (preferred):** `package.json` with a `deploy:android` script that runs the full build and outputs `app-debug.apk` to the standard Capacitor path.

**Option B (legacy):** presence of `scripts/linux-android-debug.mjs` or `scripts/linux-android-debug.sh`.

The project must also have a `src-capacitor/android/` or `android/` directory.

### Stage markers (for live progress)

Build scripts should emit stage markers to stdout:

```bash
echo "=== [1/5] Installing dependencies ==="
# ... do work ...
echo "=== [2/5] Building web bundle ==="
# ...
echo "=== [5/5] Compiling APK ==="
```

Or the `==>` prefix used by some scripts:
```bash
echo "==> Installing dependencies"
```

### APK delivery

The build service handles APK delivery to TailDrive automatically after each build.
Scripts do **not** need to copy APKs to Dropbox — if the copy step fails (because Dropbox
isn't mounted), the build service finds the APK and uploads it directly.

---

## Agent usage

### Hermes (access at localhost:8083)

```
Build all Android APKs:
  POST http://localhost:8083/build
  Body: {"projects": "all", "parallel": 2}

Then poll every 30 seconds:
  GET http://localhost:8083/status

Report to user: "Building X projects. Currently active: {project} at stage N/M ({stage_name}). Completed: Y. Failed: Z."
```

### OpenClaw (access via TailScale IP of ms-surface-1, port 8083)

Same API, different base URL.

---

## Volumes

| Volume | Container path | Purpose |
|--------|---------------|---------|
| `C:/vue.js` | `/mount/vue` | Vue.js project source |
| `android-sdk` | `/mount/android-sdk` | Android SDK (persisted across restarts) |
| `android-gradle` | `/root/.gradle` | Gradle dependency cache |
| `android-builder-logs` | `/mount/logs` | Build logs (kept last 10 per project) |

---

## New project setup

1. Create your Vue.js/Capacitor project in `C:\vue.js\<project-name>\`
2. Add to `package.json` scripts:
   ```json
   "deploy:android": "node scripts/linux-android-debug.mjs"
   ```
3. In your build script, emit stage markers (`=== [N/M] Stage Name ===`)
4. Run `GET /projects` to confirm discovery

The android-builder will find and build it automatically on the next `POST /build`.
