"""
android-builder: build orchestration for Vue.js/Capacitor Android projects.

Each project must have one of:
  A) package.json scripts.deploy:android   (preferred)
  B) scripts/linux-android-debug.mjs
  C) scripts/deploy-android.cross-platform.mjs

Stage markers emitted to stdout (any of these trigger progress updates):
  === [N/M] Stage Name ===
  ==> Stage Name
"""

import glob
import json
import os
import re
import subprocess
import threading
import time
import urllib.request
from dataclasses import dataclass, field
from typing import Optional

# ── Config ─────────────────────────────────────────────────────────────────
PROJECTS_ROOT = os.environ.get("PROJECTS_ROOT", "/mount/vue")
LOGS_DIR = os.environ.get("LOGS_DIR", "/mount/logs")
TAILDRIVE_BASE = os.environ.get(
    "TAILDRIVE_URL",
    "http://100.100.100.100:8080/atkins.email@gmail.com/zephyrusg16/dropboxapps",
)
ANDROID_HOME = os.environ.get("ANDROID_HOME", "/mount/android-sdk")
JAVA_HOME = os.environ.get("JAVA_HOME", "/usr/lib/jvm/java-17-openjdk-amd64")
MAX_LOGS_PER_PROJECT = 10

# Regex patterns for stage progress
STAGE_RE = re.compile(r"===\s*\[(\d+)/(\d+)\]\s+(.+?)\s*===")
STAGE_ARROW_RE = re.compile(r"^==>\s+(.+)$")


# ── State dataclasses ───────────────────────────────────────────────────────
@dataclass
class ActiveBuild:
    project: str
    stage: int = 0
    total_stages: int = 0
    stage_name: str = "Starting"
    started_at: float = field(default_factory=time.time)
    log_path: str = ""


@dataclass
class CompletedBuild:
    project: str
    version: str
    apk_size_mb: float
    duration_s: float
    log_path: str
    taildrive_url: str = ""


@dataclass
class FailedBuild:
    project: str
    stage_failed: int
    stage_name: str
    error: str
    duration_s: float
    log_path: str


class BuildState:
    def __init__(self):
        self._lock = threading.Lock()
        self.running = False
        self.job_id: Optional[str] = None
        self.started_at: Optional[float] = None
        self.max_parallel: int = 1
        self.queue: list = []
        self.active: list = []
        self.completed: list = []
        self.failed: list = []

    def to_dict(self) -> dict:
        with self._lock:
            return {
                "running": self.running,
                "job_id": self.job_id,
                "started_at": self.started_at,
                "max_parallel": self.max_parallel,
                "queue": list(self.queue),
                "active": [
                    {
                        "project": b.project,
                        "stage": b.stage,
                        "total_stages": b.total_stages,
                        "stage_name": b.stage_name,
                        "elapsed_s": int(time.time() - b.started_at),
                    }
                    for b in self.active
                ],
                "completed": [
                    {
                        "project": b.project,
                        "version": b.version,
                        "apk_size_mb": b.apk_size_mb,
                        "duration_s": int(b.duration_s),
                        "taildrive_url": b.taildrive_url,
                    }
                    for b in self.completed
                ],
                "failed": [
                    {
                        "project": b.project,
                        "stage_failed": b.stage_failed,
                        "stage_name": b.stage_name,
                        "error": b.error[:800],
                        "duration_s": int(b.duration_s),
                    }
                    for b in self.failed
                ],
                "summary": {
                    "completed": len(self.completed),
                    "failed": len(self.failed),
                    "active": len(self.active),
                    "queued": len(self.queue),
                },
            }


state = BuildState()


# ── Project discovery ───────────────────────────────────────────────────────
def _read_pkg(project_dir: str) -> Optional[dict]:
    pkg_path = os.path.join(project_dir, "package.json")
    if not os.path.isfile(pkg_path):
        return None
    try:
        with open(pkg_path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def discover_projects() -> list:
    """Scan PROJECTS_ROOT and return list of buildable project info dicts."""
    projects = []
    if not os.path.isdir(PROJECTS_ROOT):
        return projects

    for entry in sorted(os.listdir(PROJECTS_ROOT)):
        project_dir = os.path.join(PROJECTS_ROOT, entry)
        if not os.path.isdir(project_dir):
            continue

        pkg = _read_pkg(project_dir)
        if pkg is None:
            continue

        scripts = pkg.get("scripts", {})
        build_method = None

        if "deploy:android" in scripts:
            build_method = "npm:deploy:android"
        elif "android:debug" in scripts:
            build_method = "npm:android:debug"
        else:
            for script in [
                "scripts/linux-android-debug.mjs",
                "scripts/deploy-android.cross-platform.mjs",
                "scripts/linux-android-debug.sh",
            ]:
                if os.path.isfile(os.path.join(project_dir, script)):
                    build_method = f"script:{script}"
                    break

        # Must have Android directory to be considered buildable
        has_android = os.path.isdir(os.path.join(project_dir, "src-capacitor")) or \
                      os.path.isdir(os.path.join(project_dir, "android"))

        if build_method and has_android:
            projects.append({
                "name": entry,
                "path": project_dir,
                "build_method": build_method,
                "version": pkg.get("version", "0.0.0"),
                "product_name": pkg.get("productName", _infer_product_name(entry)),
            })

    return projects


def _infer_product_name(dir_name: str) -> str:
    """Convert directory name to TitleCase product name."""
    parts = re.split(r"[-_]", dir_name)
    return "".join(p.capitalize() for p in parts)


# ── Build command resolution ────────────────────────────────────────────────
def _get_build_command(project_info: dict) -> tuple:
    """Return (command_list, cwd) for the given project."""
    path = project_info["path"]
    method = project_info.get("build_method", "")

    if method.startswith("npm:"):
        script = method[4:]  # e.g. "deploy:android"
        return ["npm", "run", script], path

    if method.startswith("script:"):
        script_file = method[7:]  # e.g. "scripts/linux-android-debug.mjs"
        if script_file.endswith(".mjs") or script_file.endswith(".js"):
            return ["node", script_file], path
        else:
            return ["bash", script_file], path

    # Fallback: try npm run deploy:android
    return ["npm", "run", "deploy:android"], path


# ── APK finding and TailDrive delivery ─────────────────────────────────────
APK_SEARCH_PATHS = [
    "src-capacitor/android/app/build/outputs/apk/debug/app-debug.apk",
    "android/app/build/outputs/apk/debug/app-debug.apk",
]


def _find_apk(project_path: str) -> Optional[str]:
    for rel in APK_SEARCH_PATHS:
        candidate = os.path.join(project_path, rel)
        if os.path.isfile(candidate):
            return candidate
    return None


def _upload_to_taildrive(apk_path: str, product_name: str, version: str) -> tuple:
    """
    Upload APK to TailDrive via WebDAV PUT.
    Returns (success: bool, url_or_error: str)
    """
    from urllib.parse import quote

    filename = f"{product_name} {version}.apk"
    dir_url = f"{TAILDRIVE_BASE}/{quote(product_name)}/"
    file_url = f"{TAILDRIVE_BASE}/{quote(product_name)}/{quote(filename)}"

    try:
        # Ensure destination folder exists (MKCOL - ignore errors if already exists)
        try:
            req = urllib.request.Request(dir_url, method="MKCOL")
            urllib.request.urlopen(req, timeout=15)
        except Exception:
            pass  # Folder likely already exists

        # PUT the APK
        apk_size = os.path.getsize(apk_path)
        with open(apk_path, "rb") as f:
            data = f.read()

        req = urllib.request.Request(file_url, data=data, method="PUT")
        req.add_header("Content-Type", "application/octet-stream")
        req.add_header("Content-Length", str(apk_size))
        urllib.request.urlopen(req, timeout=120)

        return True, file_url

    except Exception as e:
        return False, str(e)


# ── Log management ─────────────────────────────────────────────────────────
def _get_log_path(project_name: str) -> str:
    os.makedirs(LOGS_DIR, exist_ok=True)
    ts = time.strftime("%Y%m%d_%H%M%S")
    return os.path.join(LOGS_DIR, f"{project_name}_{ts}.log")


def _rotate_logs(project_name: str):
    pattern = os.path.join(LOGS_DIR, f"{project_name}_*.log")
    logs = sorted(glob.glob(pattern))
    while len(logs) >= MAX_LOGS_PER_PROJECT:
        try:
            os.remove(logs.pop(0))
        except Exception:
            break


# ── Single project build ────────────────────────────────────────────────────
def _build_project(project_info: dict, active_build: ActiveBuild) -> tuple:
    """
    Run the build for one project.
    Returns (success: bool, error_msg: str, apk_path: Optional[str]).
    """
    path = project_info["path"]
    name = project_info["name"]
    cmd, cwd = _get_build_command(project_info)

    # Build environment
    env = os.environ.copy()
    env["ANDROID_HOME"] = ANDROID_HOME
    env["JAVA_HOME"] = JAVA_HOME
    extra = [
        f"{ANDROID_HOME}/cmdline-tools/latest/bin",
        f"{ANDROID_HOME}/platform-tools",
        f"{ANDROID_HOME}/build-tools/34.0.0",
        f"{JAVA_HOME}/bin",
    ]
    env["PATH"] = ":".join(extra) + ":" + env.get("PATH", "")
    # Hint: allow scripts that support ANDROID_APK_DEST_DIR to skip Dropbox
    env["ANDROID_APK_DEST_DIR"] = "/tmp/apk-output"

    log_path = active_build.log_path
    last_lines: list = []

    try:
        os.makedirs("/tmp/apk-output", exist_ok=True)

        with open(log_path, "a", encoding="utf-8") as log_f:
            log_f.write(f"=== BUILD START: {name} ===\n")
            log_f.write(f"Command: {' '.join(cmd)}\n")
            log_f.write(f"CWD: {cwd}\n\n")
            log_f.flush()

            proc = subprocess.Popen(
                cmd,
                cwd=cwd,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                errors="replace",
            )

            for line in proc.stdout:
                log_f.write(line)
                log_f.flush()
                stripped = line.rstrip()
                last_lines.append(stripped)
                if len(last_lines) > 100:
                    last_lines.pop(0)

                # Parse === [N/M] Stage Name ===
                m = STAGE_RE.search(line)
                if m:
                    with state._lock:
                        active_build.stage = int(m.group(1))
                        active_build.total_stages = int(m.group(2))
                        active_build.stage_name = m.group(3).strip()
                    continue

                # Parse ==> Stage Name (fallback for scripts like easy-ai-blogging-ui)
                m2 = STAGE_ARROW_RE.match(line)
                if m2:
                    with state._lock:
                        active_build.stage_name = m2.group(1).strip()

            proc.wait()
            exit_code = proc.returncode
            log_f.write(f"\n=== BUILD END: exit code {exit_code} ===\n")

    except Exception as e:
        return False, str(e), None

    # Check if APK was produced (may have been built even if copy step failed)
    apk_path = _find_apk(path)

    if apk_path:
        return True, "", apk_path
    else:
        error_tail = "\n".join(last_lines[-30:])
        return False, f"Exit code {exit_code}. No APK found.\n{error_tail}", None


# ── Batch orchestration ─────────────────────────────────────────────────────
def _run_batch(projects_to_build: list, max_parallel: int, job_id: str):
    """Background thread: orchestrate builds with a semaphore."""
    sem = threading.Semaphore(max_parallel)
    threads = []

    def build_one(project_info: dict):
        with sem:
            name = project_info["name"]
            product_name = project_info.get("product_name", _infer_product_name(name))

            _rotate_logs(name)
            log_path = _get_log_path(name)
            active = ActiveBuild(project=name, log_path=log_path)

            with state._lock:
                state.queue = [q for q in state.queue if q != name]
                state.active.append(active)

            start = time.time()
            success, error, apk_path = _build_project(project_info, active)
            duration = time.time() - start

            taildrive_url = ""
            if success and apk_path:
                version = project_info.get("version", "0.0.0")
                # Re-read version in case it was bumped during build
                try:
                    fresh_pkg = _read_pkg(project_info["path"])
                    if fresh_pkg:
                        version = fresh_pkg.get("version", version)
                except Exception:
                    pass

                apk_size_mb = round(os.path.getsize(apk_path) / 1024 / 1024, 2)

                # Upload to TailDrive
                ok, result = _upload_to_taildrive(apk_path, product_name, version)
                with open(log_path, "a", encoding="utf-8") as lf:
                    if ok:
                        taildrive_url = result
                        lf.write(f"\n=== APK delivered to TailDrive: {result} ===\n")
                    else:
                        lf.write(f"\n=== TailDrive upload failed: {result} ===\n")
                        lf.write(f"=== APK available locally: {apk_path} ===\n")

                with state._lock:
                    state.active = [a for a in state.active if a.project != name]
                    state.completed.append(CompletedBuild(
                        project=name,
                        version=version,
                        apk_size_mb=apk_size_mb,
                        duration_s=duration,
                        log_path=log_path,
                        taildrive_url=taildrive_url,
                    ))
            else:
                with state._lock:
                    state.active = [a for a in state.active if a.project != name]
                    state.failed.append(FailedBuild(
                        project=name,
                        stage_failed=active.stage,
                        stage_name=active.stage_name,
                        error=error,
                        duration_s=duration,
                        log_path=log_path,
                    ))

    for p in projects_to_build:
        t = threading.Thread(target=build_one, args=(p,), daemon=True)
        threads.append(t)
        t.start()

    for t in threads:
        t.join()

    with state._lock:
        state.running = False


def start_batch(project_names=None, max_parallel: int = 3) -> tuple:
    """
    Start a batch build.
    project_names: None = all projects, or list of project dir names.
    Returns (success: bool, job_id_or_error: str).
    """
    with state._lock:
        if state.running:
            return False, "A build is already running — check /status"

        all_projects = discover_projects()
        if not all_projects:
            return False, f"No buildable projects found in {PROJECTS_ROOT}"

        if project_names:
            by_name = {p["name"]: p for p in all_projects}
            projects_to_build = [by_name[n] for n in project_names if n in by_name]
            not_found = [n for n in project_names if n not in by_name]
            if not_found:
                return False, f"Projects not found: {not_found}"
        else:
            projects_to_build = all_projects

        if not projects_to_build:
            return False, "No matching buildable projects"

        import uuid
        job_id = str(uuid.uuid4())[:8]
        state.running = True
        state.job_id = job_id
        state.started_at = time.time()
        state.max_parallel = max_parallel
        state.queue = [p["name"] for p in projects_to_build]
        state.active = []
        state.completed = []
        state.failed = []

    t = threading.Thread(
        target=_run_batch,
        args=(projects_to_build, max_parallel, job_id),
        daemon=True,
    )
    t.start()

    return True, job_id


def get_log_files(project_name: str) -> list:
    """Return list of log file paths for a project (newest last)."""
    pattern = os.path.join(LOGS_DIR, f"{project_name}_*.log")
    return sorted(glob.glob(pattern))
