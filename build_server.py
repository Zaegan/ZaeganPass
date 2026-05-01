#!/usr/bin/env python3
"""
Android Build Server
Listens on port 8180 for build job requests from OpenClaw.
Supports two build types:
  - react-native: Full React Native scaffold initialization and build
  - android-native: Pure Java/Kotlin Android app built with system Gradle

Builds are serialized via a single worker queue.
Returns compiler output and success/failure status.
"""

import http.server
import json
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
import hashlib
from pathlib import Path
from urllib.parse import urlparse

# ── Configuration ─────────────────────────────────────────────────────────────

PORT = 8180
GITHUB_ACCOUNT = "Zaegan"
WORK_DIR = Path.home() / "build_server" / "workspace"
CACHE_DIR = Path.home() / "build_server" / "cache"
JOBS_DIR = Path.home() / "build_server" / "jobs"
ANDROID_SDK = Path.home() / "android-sdk"
NVM_DIR = Path.home() / ".nvm"
NVM_INSTALL_URL = "https://raw.githubusercontent.com/nvm-sh/nvm/v0.39.7/install.sh"
NODE_VERSION = "20"

# ── Dependency patches registry ───────────────────────────────────────────────

DEPENDENCY_PATCHES = {
    "react-native-zip-archive": {
        "file": "node_modules/react-native-zip-archive/android/src/main/java/com/rnziparchive/RNZipArchiveModule.java",
        "find": "switch (compressionLevel)",
        "replace": "switch ((int) compressionLevel)"
    }
}

# ── Helpers ───────────────────────────────────────────────────────────────────

def log(job_id, message):
    line = f"[{job_id}] {message}"
    print(line, flush=True)
    log_path = JOBS_DIR / job_id / "log.txt"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with open(log_path, "a") as f:
        f.write(line + "\n")


def run(cmd, cwd=None, env=None, job_id=None):
    """Run a shell command, stream output to job log, return (returncode, stdout+stderr)."""
    full_env = os.environ.copy()
    if env:
        full_env.update(env)
    proc = subprocess.Popen(
        cmd, shell=True, cwd=cwd, env=full_env,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True
    )
    output_lines = []
    for line in proc.stdout:
        line = line.rstrip()
        output_lines.append(line)
        if job_id:
            log(job_id, line)
    proc.wait()
    return proc.returncode, "\n".join(output_lines)


def base_env():
    """Return environment dict with Android SDK set."""
    env = os.environ.copy()
    env["ANDROID_HOME"] = str(ANDROID_SDK)
    env["ANDROID_SDK_ROOT"] = str(ANDROID_SDK)
    return env


def node_env(node_version=None):
    """Return environment dict with nvm Node on PATH and Android SDK set."""
    node_bin = NVM_DIR / "versions" / "node" / f"v{get_installed_node_version(node_version)}" / "bin"
    env = base_env()
    env["PATH"] = str(node_bin) + ":" + env.get("PATH", "")
    return env


def get_installed_node_version(node_version=None):
    """Return the installed Node version string under nvm."""
    target = node_version or NODE_VERSION
    versions_dir = NVM_DIR / "versions" / "node"
    if not versions_dir.exists():
        return None
    versions = sorted(versions_dir.iterdir(), reverse=True)
    for v in versions:
        if v.name.startswith(f"v{target}"):
            return v.name[1:]  # strip leading 'v'
    return None


def derive_package_name(repo_name):
    """Derive Android package name from GitHub repo name.
    github.com/Zaegan/BurnerPad  -> com.github.zaegan.burnerpad
    github.com/Zaegan/my-app     -> com.github.zaegan.my_app
    Hyphens are replaced with underscores; result is always a valid Java identifier.
    """
    safe_name = repo_name.lower().replace("-", "_")
    return f"com.github.zaegan.{safe_name}"


def manifest_hash(manifest):
    """Return a hash of the build manifest for change detection."""
    canonical = json.dumps(manifest, sort_keys=True)
    return hashlib.sha256(canonical.encode()).hexdigest()


def pkg_path(pkg_name):
    """Convert package name to path components."""
    return Path(*pkg_name.split("."))


# ── Dependency checks ─────────────────────────────────────────────────────────

def check_system_dependencies():
    """Check for system dependencies that require sudo to install.
    Returns list of error messages if any are missing."""
    errors = []
    missing = []

    checks = {
        "git": ("git --version", "git"),
        "java": ("java -version", "default-jdk"),
        "imagemagick": ("convert --version", "imagemagick"),
    }

    for name, (cmd, pkg) in checks.items():
        rc, _ = run(cmd)
        if rc != 0:
            missing.append((name, pkg))

    if not ANDROID_SDK.exists():
        errors.append(
            "Android SDK not found at ~/android-sdk.\n"
            "Install Android command-line tools from https://developer.android.com/studio#command-tools\n"
            "and set up the SDK at ~/android-sdk."
        )

    # Check system Gradle (required for android-native builds)
    rc, out = run("gradle --version")
    if rc != 0:
        errors.append(
            "System Gradle not found.\n"
            "Install via SDKMAN as a sudo-capable user:\n\n"
            "    curl -s https://get.sdkman.io | bash\n"
            "    source ~/.sdkman/bin/sdkman-init.sh\n"
            "    sdk install gradle"
        )

    if missing:
        pkgs = " ".join(pkg for _, pkg in missing)
        errors.append(
            f"Missing system packages: {', '.join(n for n, _ in missing)}\n"
            f"Install as a sudo user with:\n\n"
            f"    sudo apt update && sudo apt install {pkgs}"
        )

    return errors


def ensure_nvm(job_id="startup"):
    """Install nvm if not present."""
    if NVM_DIR.exists():
        log(job_id, "nvm already installed.")
        return True

    log(job_id, "nvm not found. Installing...")
    rc, out = run(f"curl -o- {NVM_INSTALL_URL} | bash", job_id=job_id)
    if rc != 0:
        log(job_id, f"ERROR: nvm installation failed:\n{out}")
        return False
    log(job_id, "nvm installed successfully.")
    return True


def ensure_node(job_id="startup", node_version=None):
    """Install Node via nvm if not present."""
    target = node_version or NODE_VERSION
    if get_installed_node_version(target):
        log(job_id, f"Node {target} already installed.")
        return True

    if not ensure_nvm(job_id):
        return False

    log(job_id, f"Installing Node {target} via nvm...")
    nvm_sh = NVM_DIR / "nvm.sh"
    rc, out = run(f"bash -c 'source {nvm_sh} && nvm install {target}'", job_id=job_id)
    if rc != 0:
        log(job_id, f"ERROR: Node installation failed:\n{out}")
        return False
    log(job_id, f"Node {target} installed successfully.")
    return True


# ── Shared helpers ────────────────────────────────────────────────────────────

def clone_or_pull(repo_name, ref, job_id):
    """Clone repo if not present, otherwise fetch and reset to ref."""
    repo_dir = WORK_DIR / repo_name / "repo"
    repo_url = f"https://github.com/{GITHUB_ACCOUNT}/{repo_name}.git"

    repo_dir.parent.mkdir(parents=True, exist_ok=True)
    if not repo_dir.exists():
        log(job_id, f"Cloning {repo_url}...")
        rc, out = run(f"git clone {repo_url} {repo_dir}", job_id=job_id)
        if rc != 0:
            return None, f"Clone failed:\n{out}"
    else:
        log(job_id, f"Fetching latest from {repo_url}...")
        rc, out = run("git fetch origin", cwd=repo_dir, job_id=job_id)
        if rc != 0:
            return None, f"Fetch failed:\n{out}"

    log(job_id, f"Checking out {ref}...")
    rc, out = run(f"git checkout {ref}", cwd=repo_dir, job_id=job_id)
    if rc != 0:
        rc, out = run(f"git checkout origin/{ref}", cwd=repo_dir, job_id=job_id)
        if rc != 0:
            return None, f"Checkout of {ref} failed:\n{out}"

    log(job_id, f"Resetting to origin/{ref}...")
    rc, out = run(f"git reset --hard origin/{ref}", cwd=repo_dir, job_id=job_id)
    if rc != 0:
        return None, f"Reset to origin/{ref} failed:\n{out}"

    return repo_dir, None


def load_manifest(repo_dir):
    """Load and validate build.json from repo root."""
    manifest_path = repo_dir / "build.json"
    if not manifest_path.exists():
        return None, "build.json not found in repo root."
    try:
        with open(manifest_path) as f:
            manifest = json.load(f)
    except json.JSONDecodeError as e:
        return None, f"build.json is invalid JSON: {e}"

    # type field required for all builds
    build_type = manifest.get("type")
    if not build_type:
        return None, "build.json missing required field: type (must be 'react-native' or 'android-native')"
    if build_type not in ("react-native", "android-native"):
        return None, f"build.json: unknown type '{build_type}' (must be 'react-native' or 'android-native')"

    # Common required fields
    common_required = ["compile_sdk", "target_sdk", "min_sdk", "dependencies"]
    missing = [k for k in common_required if k not in manifest]

    # React Native specific
    if build_type == "react-native" and "react_native_version" not in manifest:
        missing.append("react_native_version")

    if missing:
        return None, f"build.json missing required fields: {', '.join(missing)}"

    # Apply defaults for optional fields
    manifest.setdefault("version_code", 1)
    manifest.setdefault("version_name", "1.0")

    error = validate_manifest(manifest)
    if error:
        return None, error

    return manifest, None


# Allowlist patterns for manifest field validation
_RE_VERSION_SEGMENT = re.compile(r'^\d{1,6}$')
_RE_VERSION_NAME    = re.compile(r'^[A-Za-z0-9][A-Za-z0-9.\-+_]{0,31}$')
_RE_SDK_VERSION     = re.compile(r'^\d{1,3}$')
_RE_JAVA_VERSION    = re.compile(r'^(1\.)?(\d{1,2})$')
_RE_PERMISSION      = re.compile(r'^[A-Za-z][A-Za-z0-9_.]{0,127}$')
_RE_APP_NAME        = re.compile(r'^[A-Za-z0-9][A-Za-z0-9 _.&\-]{0,48}$')
_RE_VERSION_DOTS    = re.compile(r'^\d+(\.\d+){1,3}$')
_RE_NPM_DEP         = re.compile(r'^@?[a-zA-Z0-9][a-zA-Z0-9._\-/]*(@[a-zA-Z0-9.\-^~*_]+)?$')
_RE_GRADLE_DEP      = re.compile(r'^[a-zA-Z0-9._\-]+:[a-zA-Z0-9._\-]+:[a-zA-Z0-9._\-+]+$')
_RE_SOURCE_DIR      = re.compile(r'^[a-zA-Z0-9_.\-/]+$')
_RE_IDENTIFIER      = re.compile(r'^[a-zA-Z0-9_\-]+$')
_JAVA_VERSIONS_OK   = {"8", "11", "17", "21", "1.8"}


def validate_manifest(manifest):
    """
    Validate all manifest fields against strict allowlists.
    Returns an error string if any field is invalid, otherwise None.
    Prevents shell injection, XML injection, and path traversal via build.json.
    """
    build_type = manifest["type"]

    # --- version_code: positive integer ---
    vc = manifest["version_code"]
    if not isinstance(vc, int) or vc < 1:
        return f"build.json: version_code must be a positive integer, got {vc!r}"

    # --- version_name: safe version string ---
    vn = str(manifest["version_name"])
    if not _RE_VERSION_NAME.match(vn):
        return f"build.json: version_name contains disallowed characters: {vn!r}"

    # --- SDK integers ---
    for field in ("compile_sdk", "target_sdk", "min_sdk"):
        v = manifest.get(field)
        if v is not None:
            if not isinstance(v, int) or not (1 <= v <= 99):
                return f"build.json: {field} must be an integer between 1 and 99, got {v!r}"

    # --- agp_version: digits and dots ---
    agp = manifest.get("agp_version")
    if agp is not None and not _RE_VERSION_DOTS.match(str(agp)):
        return f"build.json: agp_version contains disallowed characters: {agp!r}"

    # --- react_native_version: digits and dots ---
    rnv = manifest.get("react_native_version")
    if rnv is not None and not _RE_VERSION_DOTS.match(str(rnv)):
        return f"build.json: react_native_version contains disallowed characters: {rnv!r}"

    # --- node_version: digits and dots ---
    nv = manifest.get("node_version")
    if nv is not None and not _RE_VERSION_DOTS.match(str(nv)) and not _RE_VERSION_SEGMENT.match(str(nv)):
        return f"build.json: node_version contains disallowed characters: {nv!r}"

    # --- java_version: known safe set ---
    jv = manifest.get("java_version")
    if jv is not None and str(jv) not in _JAVA_VERSIONS_OK:
        return f"build.json: java_version must be one of {sorted(_JAVA_VERSIONS_OK)}, got {jv!r}"

    # --- app_name: safe display string ---
    app_name = manifest.get("app_name")
    if app_name is not None and not _RE_APP_NAME.match(str(app_name)):
        return f"build.json: app_name contains disallowed characters: {app_name!r}"

    # --- permissions: Android permission name format ---
    for p in manifest.get("permissions", []):
        if not isinstance(p, str) or not _RE_PERMISSION.match(p):
            return f"build.json: invalid permission name: {p!r}"

    # --- dependencies ---
    deps = manifest.get("dependencies", [])
    if not isinstance(deps, list):
        return "build.json: dependencies must be a list"
    if build_type == "react-native":
        for d in deps:
            if not isinstance(d, str) or not _RE_NPM_DEP.match(d):
                return f"build.json: invalid npm dependency: {d!r}"
    else:
        for d in deps:
            if not isinstance(d, str) or not _RE_GRADLE_DEP.match(d):
                return f"build.json: invalid Gradle dependency: {d!r}"

    # --- source_dirs: no path traversal ---
    for d in manifest.get("source_dirs", []):
        if not isinstance(d, str) or not _RE_SOURCE_DIR.match(d) or ".." in d:
            return f"build.json: invalid source_dir: {d!r}"

    # --- modules: safe identifiers + validate nested fields ---
    for m in manifest.get("modules", []):
        name = m if isinstance(m, str) else m.get("name", "")
        if not _RE_IDENTIFIER.match(str(name)):
            return f"build.json: invalid module name: {name!r}"
        if isinstance(m, dict):
            jv = m.get("java_version")
            if jv is not None and str(jv) not in _JAVA_VERSIONS_OK:
                return f"build.json: module '{name}': java_version must be one of {sorted(_JAVA_VERSIONS_OK)}, got {jv!r}"
            for d in m.get("dependencies", []):
                if not isinstance(d, str) or not _RE_GRADLE_DEP.match(d):
                    return f"build.json: module '{name}': invalid Gradle dependency: {d!r}"

    # --- native_modules: safe identifiers ---
    for m in manifest.get("native_modules", []):
        name = m if isinstance(m, str) else m.get("name", "")
        if not _RE_IDENTIFIER.match(str(name)):
            return f"build.json: invalid native_module name: {name!r}"

    return None


def needs_clean_build(repo_name, manifest):
    """Return True if manifest has changed since last successful build."""
    cache_path = CACHE_DIR / repo_name / "last_build.json"
    if not cache_path.exists():
        return True
    try:
        with open(cache_path) as f:
            cached = json.load(f)
        return manifest_hash(cached) != manifest_hash(manifest)
    except Exception:
        return True


def save_manifest_cache(repo_name, manifest):
    """Save manifest after a successful build."""
    cache_path = CACHE_DIR / repo_name / "last_build.json"
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    with open(cache_path, "w") as f:
        json.dump(manifest, f, indent=2)


def generate_icons(repo_name, res_dir, job_id):
    """Generate icon density variants from icon-512.png into given res dir."""
    icon_src = WORK_DIR / repo_name / "repo" / "icon-512.png"
    if not icon_src.exists():
        return "icon-512.png not found in repo root"

    densities = {
        "mdpi": 48, "hdpi": 72, "xhdpi": 96, "xxhdpi": 144, "xxxhdpi": 192
    }
    log(job_id, "Generating icon density variants...")
    for density, size in densities.items():
        target_dir = res_dir / f"mipmap-{density}"
        target_dir.mkdir(parents=True, exist_ok=True)

        rc, out = run(
            f'convert "{icon_src}" -resize {size}x{size} "{target_dir}/ic_launcher.png"',
            job_id=job_id
        )
        if rc != 0:
            return f"Icon generation failed for {density}: {out}"

        rc, out = run(
            f'convert "{icon_src}" -resize {size}x{size} '
            f'\\( +clone -alpha extract '
            f'-draw "fill black polygon 0,0 0,{size} {size},0 fill white circle {size//2},{size//2} {size//2},0" '
            f'\\( +clone -flip \\) -compose Multiply -composite '
            f'\\( +clone -flop \\) -compose Multiply -composite '
            f'\\) -alpha off -compose CopyOpacity -composite '
            f'"{target_dir}/ic_launcher_round.png"',
            job_id=job_id
        )
        if rc != 0:
            return f"Round icon generation failed for {density}: {out}"

    log(job_id, "Icons generated successfully.")
    return None


# ── React Native build path ───────────────────────────────────────────────────

def rn_scaffold_dir(repo_name):
    return WORK_DIR / repo_name / "scaffold"



def rn_init(repo_name, manifest, job_id):
    """Run full React Native project initialization."""
    app_dir = rn_scaffold_dir(repo_name)
    rn_version = manifest["react_native_version"]
    pkg_name = derive_package_name(repo_name)
    app_name = f"{repo_name}App"

    if app_dir.exists():
        log(job_id, f"Removing existing scaffold at {app_dir}...")
        shutil.rmtree(app_dir)

    log(job_id, f"Initializing React Native {rn_version} project...")
    node_version = manifest.get("node_version")
    env = node_env(node_version)
    rc, out = run(
        f"npx @react-native-community/cli init {app_name} --version {rn_version}",
        cwd=WORK_DIR, env=env, job_id=job_id
    )
    if rc != 0:
        return f"React Native init failed:\n{out}"

    # npx init creates WORK_DIR/{app_name}; move it into the consolidated project dir
    created_dir = WORK_DIR / app_name
    if created_dir != app_dir:
        app_dir.parent.mkdir(parents=True, exist_ok=True)
        created_dir.rename(app_dir)

    # Patch package name, namespace, and version fields
    gradle_path = app_dir / "android" / "app" / "build.gradle"
    log(job_id, "Patching package name, namespace, and version...")
    content = gradle_path.read_text()
    content = re.sub(r'namespace "com\.\w+"', f'namespace "{pkg_name}"', content)
    content = re.sub(r'applicationId "com\.\w+"', f'applicationId "{pkg_name}"', content)
    content = re.sub(r'versionCode \d+', f'versionCode {manifest["version_code"]}', content)
    content = re.sub(r'versionName "[^"]+"', f'versionName "{manifest["version_name"]}"', content)
    if "compile_sdk" in manifest:
        content = re.sub(r'compileSdkVersion \d+', f'compileSdkVersion {manifest["compile_sdk"]}', content)
        content = re.sub(r'compileSdk \d+', f'compileSdk {manifest["compile_sdk"]}', content)
    if "target_sdk" in manifest:
        content = re.sub(r'targetSdkVersion \d+', f'targetSdkVersion {manifest["target_sdk"]}', content)
        content = re.sub(r'targetSdk \d+', f'targetSdk {manifest["target_sdk"]}', content)
    if "min_sdk" in manifest:
        content = re.sub(r'minSdkVersion \d+', f'minSdkVersion {manifest["min_sdk"]}', content)
        content = re.sub(r'minSdk \d+', f'minSdk {manifest["min_sdk"]}', content)
    # Strip signing from release so it stays unsigned (for user to sign with their own key)
    content = re.sub(r'\s*signingConfig signingConfigs\.debug\n', '\n', content)
    # Append a debug-signed variant for sideload testing
    content += """
// Debug-signed release variant for sideload testing
android {
    buildTypes {
        releaseSigned {
            initWith(buildTypes.release)
            signingConfig signingConfigs.debug
            matchingFallbacks = ['release']
            manifestPlaceholders = [usesCleartextTraffic: "false"]
        }
    }
}
"""
    gradle_path.write_text(content)

    # Patch app name in strings.xml
    strings_path = app_dir / "android" / "app" / "src" / "main" / "res" / "values" / "strings.xml"
    strings_content = strings_path.read_text()
    app_display_name = manifest.get("app_name", repo_name)
    strings_content = re.sub(
        r'<string name="app_name">.*?</string>',
        f'<string name="app_name">{app_display_name}</string>',
        strings_content
    )
    strings_path.write_text(strings_content)

    # Patch AndroidManifest.xml — resizable activity + config changes + permissions
    android_manifest_path = app_dir / "android" / "app" / "src" / "main" / "AndroidManifest.xml"
    log(job_id, "Patching AndroidManifest.xml...")
    android_manifest = android_manifest_path.read_text()
    if 'resizeableActivity' not in android_manifest:
        android_manifest = re.sub(
            r'(<activity\b)',
            r'\1\n        android:resizeableActivity="true"',
            android_manifest,
            count=1
        )
    if 'configChanges' not in android_manifest:
        android_manifest = re.sub(
            r'(<activity\b)',
            r'\1\n        android:configChanges="keyboard|keyboardHidden|orientation|screenLayout|screenSize|smallestScreenSize|uiMode"',
            android_manifest,
            count=1
        )
    permissions = manifest.get("permissions", [])
    if permissions:
        permission_xml = "\n".join(
            f'    <uses-permission android:name="{p}" />'
            for p in permissions
        )
        android_manifest = android_manifest.replace(
            '    <application',
            f'{permission_xml}\n    <application',
            1
        )
    android_manifest_path.write_text(android_manifest)

    # Move default Kotlin files to correct package path
    old_pkg_dir = app_dir / "android" / "app" / "src" / "main" / "java" / "com" / app_name.lower()
    new_pkg_dir = app_dir / "android" / "app" / "src" / "main" / "java" / pkg_path(pkg_name)
    if old_pkg_dir.exists() and old_pkg_dir != new_pkg_dir:
        log(job_id, f"Moving Kotlin package from {old_pkg_dir.name} to {new_pkg_dir}...")
        new_pkg_dir.mkdir(parents=True, exist_ok=True)
        for kt in old_pkg_dir.glob("*.kt"):
            content = kt.read_text()
            content = re.sub(r'^package com\.\w+', f'package {pkg_name}', content, flags=re.MULTILINE)
            (new_pkg_dir / kt.name).write_text(content)
        shutil.rmtree(old_pkg_dir)

    # Remove default App.tsx
    app_tsx = app_dir / "App.tsx"
    if app_tsx.exists():
        app_tsx.unlink()

    # Create extra source directories declared in build.json
    for d in manifest.get("source_dirs", []):
        (app_dir / d).mkdir(parents=True, exist_ok=True)

    # Register native modules declared in build.json
    native_modules = manifest.get("native_modules", [])
    if native_modules:
        main_app_path = new_pkg_dir / "MainApplication.kt"
        if main_app_path.exists():
            log(job_id, f"Registering native modules: {native_modules}...")
            content = main_app_path.read_text()
            for module in native_modules:
                pkg_class = f"{pkg_name}.{module}Package"
                if module + "Package" not in content:
                    content = content.replace(
                        "import com.facebook.react.PackageList",
                        f"import com.facebook.react.PackageList\nimport {pkg_class}"
                    )
                    content = content.replace(
                        "// add(MyReactNativePackage())",
                        f"add({module}Package())"
                    )
            main_app_path.write_text(content)

    # Install npm dependencies
    log(job_id, "Installing npm dependencies...")
    deps = " ".join(manifest["dependencies"])
    rc, out = run(f"npm install {deps}", cwd=app_dir, env=node_env(node_version), job_id=job_id)
    if rc != 0:
        return f"npm install failed:\n{out}"

    # Apply dependency patches
    error = rn_apply_patches(manifest["dependencies"], app_dir, job_id)
    if error:
        return error

    # Generate icons
    res_dir = app_dir / "android" / "app" / "src" / "main" / "res"
    icon_error = generate_icons(repo_name, res_dir, job_id)
    if icon_error:
        log(job_id, f"WARNING: {icon_error} — using default icon.")

    return None


def rn_apply_patches(dependencies, app_dir, job_id):
    """Apply known patches for installed React Native dependencies."""
    for dep in dependencies:
        if dep in DEPENDENCY_PATCHES:
            patch = DEPENDENCY_PATCHES[dep]
            patch_path = app_dir / patch["file"]
            if not patch_path.exists():
                log(job_id, f"WARNING: Patch target not found for {dep}: {patch['file']}")
                continue
            log(job_id, f"Applying patch for {dep}...")
            content = patch_path.read_text()
            if patch["find"] in content:
                content = content.replace(patch["find"], patch["replace"])
                patch_path.write_text(content)
                log(job_id, f"Patch applied for {dep}.")
            else:
                log(job_id, f"Patch for {dep} not needed or already applied.")
    return None


def rn_sync_source(repo_name, repo_dir, app_dir, job_id):
    """Sync React Native source files from repo into scaffold."""
    log(job_id, "Syncing source files...")

    src = repo_dir / "App.js"
    if src.exists():
        shutil.copy2(src, app_dir / "App.js")

    for tree in ["src", "android"]:
        src_tree = repo_dir / tree
        dst_tree = app_dir / tree if tree == "src" else app_dir / "android"
        if src_tree.exists():
            for src_file in src_tree.rglob("*"):
                if src_file.is_file():
                    rel = src_file.relative_to(src_tree)
                    dst_file = dst_tree / rel
                    dst_file.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(src_file, dst_file)

    log(job_id, "Source sync complete.")
    return None


def rn_build(repo_name, app_dir, job_id, node_version=None):
    """Run React Native Gradle build."""
    android_dir = app_dir / "android"
    gradlew = android_dir / "gradlew"

    if not gradlew.exists():
        return False, "gradlew not found — project may not be initialized."

    log(job_id, "Running Gradle build...")
    env = node_env(node_version)
    rc, out = run(
        "./gradlew clean assembleRelease assembleReleaseSigned bundleRelease",
        cwd=android_dir, env=env, job_id=job_id
    )
    return rc == 0, out


# ── Native Android build path ─────────────────────────────────────────────────

def native_scaffold_dir(repo_name):
    return WORK_DIR / repo_name / "scaffold"


def native_build_gradle(pkg_name, repo_name, manifest):
    """Generate server-owned app/build.gradle for a native Android project."""
    deps = manifest.get("dependencies", [])
    dep_lines = "\n".join(f'    implementation "{d}"' for d in deps)
    module_dep_lines = "\n".join(
        f"    implementation project(':{m['name']}')"
        for m in manifest.get("modules", [])
    )
    java_version = manifest.get("java_version", "11")
    # Normalise: accept "1.8", "8", "11", "17" etc.
    jv_map = {"1.8": "VERSION_1_8", "8": "VERSION_1_8"}
    jv_const = jv_map.get(java_version, f"VERSION_{java_version}")
    return f"""\
plugins {{
    id 'com.android.application'
}}

android {{
    namespace '{pkg_name}'
    compileSdk {manifest['compile_sdk']}

    defaultConfig {{
        applicationId '{pkg_name}'
        minSdk {manifest['min_sdk']}
        targetSdk {manifest['target_sdk']}
        versionCode {manifest['version_code']}
        versionName "{manifest['version_name']}"
    }}

    buildTypes {{
        release {{
            minifyEnabled false
            proguardFiles getDefaultProguardFile('proguard-android-optimize.txt'), 'proguard-rules.pro'
        }}
    }}

    compileOptions {{
        sourceCompatibility JavaVersion.{jv_const}
        targetCompatibility JavaVersion.{jv_const}
    }}
}}

dependencies {{
{dep_lines}
{module_dep_lines}
}}

// Debug-signed release variant for sideload testing
android {{
    buildTypes {{
        releaseSigned {{
            initWith(buildTypes.release)
            signingConfig signingConfigs.debug
            matchingFallbacks = ['release']
        }}
    }}
}}
"""


def native_settings_gradle(repo_name, modules=None):
    """Generate server-owned settings.gradle for a native Android project."""
    module_includes = "\n".join(f"include ':{m['name']}'" for m in (modules or []))
    return f"""\
pluginManagement {{
    repositories {{
        google()
        mavenCentral()
        gradlePluginPortal()
    }}
}}
dependencyResolutionManagement {{
    repositoriesMode.set(RepositoriesMode.FAIL_ON_PROJECT_REPOS)
    repositories {{
        google()
        mavenCentral()
    }}
}}

rootProject.name = '{repo_name}'
include ':app'
{module_includes}
"""


def native_java_library_gradle(module_spec):
    """Generate server-owned build.gradle for a java-library module."""
    deps = module_spec.get("dependencies", [])
    dep_lines = "\n".join(f'    implementation "{d}"' for d in deps)
    java_version = module_spec.get("java_version", "11")
    jv_map = {"1.8": "VERSION_1_8", "8": "VERSION_1_8"}
    jv_const = jv_map.get(java_version, f"VERSION_{java_version}")
    return f"""\
plugins {{
    id 'java-library'
}}

java {{
    sourceCompatibility = JavaVersion.{jv_const}
    targetCompatibility = JavaVersion.{jv_const}
}}

dependencies {{
{dep_lines}
}}
"""


def native_top_build_gradle(manifest):
    """Generate server-owned top-level build.gradle."""
    agp_version = manifest.get("agp_version", "8.7.0")
    return f"""\
plugins {{
    id 'com.android.application' version '{agp_version}' apply false
}}
"""


def native_gradle_properties():
    """Generate gradle.properties enabling AndroidX."""
    return """\
android.useAndroidX=true
android.enableJetifier=true
"""


def native_proguard_rules():
    """Generate default proguard rules."""
    return """\
# Default ProGuard rules for release builds.
-keepattributes *Annotation*
-keepclassmembers class * {
    @android.webkit.JavascriptInterface <methods>;
}
"""


def native_init(repo_name, manifest, job_id):
    """Set up the native Android build scaffold."""
    app_dir = native_scaffold_dir(repo_name)
    pkg_name = derive_package_name(repo_name)
    app_display_name = manifest.get("app_name", repo_name)

    if app_dir.exists():
        log(job_id, f"Removing existing native scaffold at {app_dir}...")
        shutil.rmtree(app_dir)

    log(job_id, "Setting up native Android scaffold...")
    app_dir.mkdir(parents=True)

    # Write server-owned Gradle files
    modules = manifest.get("modules", [])
    (app_dir / "settings.gradle").write_text(native_settings_gradle(repo_name, modules))
    (app_dir / "build.gradle").write_text(native_top_build_gradle(manifest))
    (app_dir / "gradle.properties").write_text(native_gradle_properties())
    (app_dir / "app").mkdir()
    (app_dir / "app" / "build.gradle").write_text(native_build_gradle(pkg_name, repo_name, manifest))
    (app_dir / "app" / "proguard-rules.pro").write_text(native_proguard_rules())

    # Create source directory structure
    src_main = app_dir / "app" / "src" / "main"
    java_dir = src_main / "java" / pkg_path(pkg_name)
    java_dir.mkdir(parents=True)
    (src_main / "res").mkdir(parents=True)

    # Minimal AndroidManifest.xml — will be overwritten by repo's manifest
    manifest_path = src_main / "AndroidManifest.xml"
    manifest_path.write_text(f"""\
<?xml version="1.0" encoding="utf-8"?>
<manifest xmlns:android="http://schemas.android.com/apk/res/android">
    <application
        android:label="{app_display_name}"
        android:theme="@style/Theme.AppCompat">
        <activity
            android:name=".MainActivity"
            android:exported="true">
            <intent-filter>
                <action android:name="android.intent.action.MAIN" />
                <category android:name="android.intent.category.LAUNCHER" />
            </intent-filter>
        </activity>
    </application>
</manifest>
""")

    # Scaffold additional library modules
    for module in modules:
        mod_name = module["name"]
        mod_type = module.get("type", "java-library")
        log(job_id, f"Scaffolding module :{mod_name} ({mod_type})...")
        mod_dir = app_dir / mod_name
        mod_dir.mkdir(parents=True)
        if mod_type == "java-library":
            (mod_dir / "build.gradle").write_text(native_java_library_gradle(module))
            (mod_dir / "src" / "main" / "java").mkdir(parents=True)
        else:
            log(job_id, f"WARNING: Unknown module type '{mod_type}' for :{mod_name} — skipping.")

    log(job_id, "Native scaffold ready.")
    return None


def native_sync_source(repo_name, repo_dir, app_dir, job_id, manifest=None):
    """Sync native Android source files from repo into scaffold."""
    log(job_id, "Syncing native source files...")

    # Sync app/src tree from repo
    src_app = repo_dir / "app" / "src"
    dst_app = app_dir / "app" / "src"
    if src_app.exists():
        for src_file in src_app.rglob("*"):
            if src_file.is_file():
                rel = src_file.relative_to(src_app)
                dst_file = dst_app / rel
                dst_file.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src_file, dst_file)
    else:
        return "app/src directory not found in repo — check repo layout convention."

    # Sync additional library module source trees
    for module in (manifest or {}).get("modules", []):
        mod_name = module["name"]
        src_mod = repo_dir / mod_name / "src"
        dst_mod = app_dir / mod_name / "src"
        if src_mod.exists():
            log(job_id, f"Syncing module :{mod_name} source...")
            for src_file in src_mod.rglob("*"):
                if src_file.is_file():
                    rel = src_file.relative_to(src_mod)
                    dst_file = dst_mod / rel
                    dst_file.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(src_file, dst_file)
        else:
            log(job_id, f"WARNING: {mod_name}/src not found in repo — module may not compile.")

    # Generate icons into res dir
    res_dir = app_dir / "app" / "src" / "main" / "res"
    icon_error = generate_icons(repo_name, res_dir, job_id)
    if icon_error:
        log(job_id, f"WARNING: {icon_error} — using default icon.")

    log(job_id, "Native source sync complete.")
    return None


def native_build(repo_name, app_dir, job_id):
    """Run system Gradle build for a native Android project."""
    log(job_id, "Running native Gradle build...")
    env = base_env()
    rc, out = run(
        "gradle clean assembleRelease assembleReleaseSigned bundleRelease",
        cwd=app_dir, env=env, job_id=job_id
    )
    return rc == 0, out


# ── Job runner ────────────────────────────────────────────────────────────────

jobs = {}
jobs_lock = threading.Lock()
job_queue = queue.Queue()


def label_artifacts(manifest, repo_name, apk_path, signed_apk_path, aab_path):
    """Rename build artifacts to include app name and version.
    Returns (apk_out, signed_apk_out, aab_out) paths regardless of existence."""
    raw_name = manifest.get("app_name", repo_name)
    app_name = re.sub(r'[^A-Za-z0-9._-]', '-', raw_name).strip('-')
    version  = re.sub(r'[^A-Za-z0-9._-]', '-', str(manifest.get("version_name", "1.0"))).strip('-')
    prefix   = f"{app_name}-{version}"

    results = []
    for src, suffix in [
        (apk_path,        f"{prefix}.apk"),
        (signed_apk_path, f"{prefix}-signed.apk"),
        (aab_path,        f"{prefix}.aab"),
    ]:
        dst = src.parent / suffix
        if src.exists():
            src.rename(dst)
        results.append(dst)
    return tuple(results)


def find_artifacts(repo_name):
    """Return sorted list of labeled artifact Paths for a repo.

    Scans both native (scaffold/app/) and RN (scaffold/android/app/) output
    directories. Excludes Gradle default names (app-release*, app-debug*)
    which indicate an unlabeled or intermediate build.
    """
    scaffold = WORK_DIR / repo_name / "scaffold"
    if not scaffold.exists():
        return []
    outputs_dirs = [
        scaffold / "app" / "build" / "outputs",
        scaffold / "android" / "app" / "build" / "outputs",
    ]
    artifacts = []
    for outputs in outputs_dirs:
        if not outputs.exists():
            continue
        for pattern in ("*.apk", "*.aab"):
            for path in outputs.rglob(pattern):
                if path.name.startswith("app-release") or path.name.startswith("app-debug"):
                    continue
                artifacts.append(path)
    return sorted(artifacts, key=lambda p: p.name)


def list_repos_with_artifacts():
    """Return sorted list of repo names that have at least one labeled artifact."""
    if not WORK_DIR.exists():
        return []
    repos = []
    for entry in sorted(WORK_DIR.iterdir()):
        if entry.is_dir() and find_artifacts(entry.name):
            repos.append(entry.name)
    return repos


def run_job(job_id, repo_name, ref, subpath, force_clean):
    with jobs_lock:
        jobs[job_id]["status"] = "running"

    success = False
    result_message = ""

    try:
        log(job_id, f"Starting build: repo={repo_name} ref={ref} subpath={subpath} force_clean={force_clean}")

        WORK_DIR.mkdir(parents=True, exist_ok=True)

        repo_dir, error = clone_or_pull(repo_name, ref, job_id)
        if error:
            raise RuntimeError(error)

        if subpath:
            repo_dir = repo_dir / subpath

        manifest, error = load_manifest(repo_dir)
        if error:
            raise RuntimeError(error)

        build_type = manifest["type"]
        log(job_id, f"Build type: {build_type}")

        if build_type == "react-native":
            node_version = manifest.get("node_version")
            if not ensure_node(job_id, node_version):
                raise RuntimeError("Node.js could not be installed.")

            app_dir = rn_scaffold_dir(repo_name)
            clean = force_clean or needs_clean_build(repo_name, manifest) or not app_dir.exists()

            if clean:
                log(job_id, "Clean build triggered.")
                error = rn_init(repo_name, manifest, job_id)
                if error:
                    raise RuntimeError(error)

            error = rn_sync_source(repo_name, repo_dir, app_dir, job_id)
            if error:
                raise RuntimeError(error)

            success, build_output = rn_build(repo_name, app_dir, job_id, node_version)

            if success:
                save_manifest_cache(repo_name, manifest)
                apk_path = app_dir / "android" / "app" / "build" / "outputs" / "apk" / "release" / "app-release.apk"
                signed_apk_path = app_dir / "android" / "app" / "build" / "outputs" / "apk" / "releaseSigned" / "app-releaseSigned.apk"
                aab_path = app_dir / "android" / "app" / "build" / "outputs" / "bundle" / "release" / "app-release.aab"
                apk_out, signed_out, aab_out = label_artifacts(manifest, repo_name, apk_path, signed_apk_path, aab_path)
                result_message = (
                    f"status: success\n"
                    f"apk: {apk_out}\n"
                    f"signed_apk: {signed_out}\n"
                    f"aab: {aab_out}"
                )
            else:
                result_message = f"status: failed\n\n{build_output}"

        elif build_type == "android-native":
            app_dir = native_scaffold_dir(repo_name)
            clean = force_clean or needs_clean_build(repo_name, manifest) or not app_dir.exists()

            if clean:
                log(job_id, "Clean build triggered.")
                error = native_init(repo_name, manifest, job_id)
                if error:
                    raise RuntimeError(error)

            error = native_sync_source(repo_name, repo_dir, app_dir, job_id, manifest)
            if error:
                raise RuntimeError(error)

            success, build_output = native_build(repo_name, app_dir, job_id)

            if success:
                save_manifest_cache(repo_name, manifest)
                apk_path = app_dir / "app" / "build" / "outputs" / "apk" / "release" / "app-release.apk"
                signed_apk_path = app_dir / "app" / "build" / "outputs" / "apk" / "releaseSigned" / "app-releaseSigned.apk"
                aab_path = app_dir / "app" / "build" / "outputs" / "bundle" / "release" / "app-release.aab"
                apk_out, signed_out, aab_out = label_artifacts(manifest, repo_name, apk_path, signed_apk_path, aab_path)
                result_message = (
                    f"status: success\n"
                    f"apk: {apk_out}\n"
                    f"signed_apk: {signed_out}\n"
                    f"aab: {aab_out}"
                )
            else:
                result_message = f"status: failed\n\n{build_output}"

        if not success:
            log(job_id, "Build FAILED.")
        else:
            log(job_id, result_message)

    except Exception as e:
        result_message = (
            f"status: error\n"
            f"message: {e}"
        )
        log(job_id, result_message)
        success = False

    with jobs_lock:
        jobs[job_id]["status"] = "complete"
        jobs[job_id]["success"] = success
        jobs[job_id]["result"] = result_message
        jobs[job_id]["finished_at"] = time.time()


def worker():
    """Single worker thread — processes one build at a time."""
    while True:
        job_id, repo_name, ref, subpath, force_clean = job_queue.get()
        try:
            run_job(job_id, repo_name, ref, subpath, force_clean)
        finally:
            job_queue.task_done()


# ── HTTP server ───────────────────────────────────────────────────────────────

class BuildHandler(http.server.BaseHTTPRequestHandler):

    def log_message(self, format, *args):
        pass  # suppress default access log

    def send_json(self, code, data):
        body = json.dumps(data, indent=2).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", len(body))
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self, code, body):
        page = (
            "<!DOCTYPE html><html><head>"
            '<meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1">'
            "<title>Build Server</title>"
            "<style>"
            "body{font-family:sans-serif;max-width:700px;margin:2rem auto;padding:0 1rem}"
            "a{color:#0070f3}ul{line-height:2}h1{margin-bottom:.5rem}"
            "</style>"
            f"</head><body>{body}</body></html>"
        )
        encoded = page.encode()
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", len(encoded))
        self.end_headers()
        self.wfile.write(encoded)

    def do_POST(self):
        if self.path == "/build":
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length)
            try:
                req = json.loads(body)
            except json.JSONDecodeError:
                self.send_json(400, {"error": "Invalid JSON"})
                return

            repo_name = req.get("repo")
            ref = req.get("ref", "main")
            subpath = req.get("subpath", "")
            force_clean = req.get("clean", False)

            if not repo_name:
                self.send_json(400, {"error": "Missing required field: repo"})
                return

            if not re.match(r'^[A-Za-z0-9_-]+$', repo_name):
                self.send_json(400, {"error": "Invalid repo name"})
                return

            job_id = f"{repo_name}-{int(time.time())}"
            with jobs_lock:
                jobs[job_id] = {
                    "job_id": job_id,
                    "repo": repo_name,
                    "ref": ref,
                    "status": "queued",
                    "success": None,
                    "result": None,
                    "created_at": time.time(),
                    "finished_at": None
                }

            job_queue.put((job_id, repo_name, ref, subpath, force_clean))

            self.send_json(202, {
                "job_id": job_id,
                "queue_position": job_queue.qsize()
            })

        else:
            self.send_json(404, {"error": "Not found"})

    def do_GET(self):
        parsed = urlparse(self.path)

        # GET /job/<id>/status
        m = re.match(r'^/job/([^/]+)/status$', parsed.path)
        if m:
            job_id = m.group(1)
            with jobs_lock:
                job = jobs.get(job_id)
            if not job:
                self.send_json(404, {"error": "Job not found"})
                return
            self.send_json(200, {
                "job_id": job_id,
                "status": job["status"],
                "elapsed": round(time.time() - job["created_at"], 1)
            })
            return

        # GET /job/<id>/result
        m = re.match(r'^/job/([^/]+)/result$', parsed.path)
        if m:
            job_id = m.group(1)
            with jobs_lock:
                job = jobs.get(job_id)
            if not job:
                self.send_json(404, {"error": "Job not found"})
                return
            if job["status"] != "complete":
                self.send_json(200, {
                    "job_id": job_id,
                    "status": job["status"],
                    "elapsed": round(time.time() - job["created_at"], 1)
                })
                return
            self.send_json(200, {
                "job_id": job_id,
                "status": "complete",
                "success": job["success"],
                "result": job["result"]
            })
            return

        # GET /job/<id>/log
        m = re.match(r'^/job/([^/]+)/log$', parsed.path)
        if m:
            job_id = m.group(1)
            log_path = JOBS_DIR / job_id / "log.txt"
            if not log_path.exists():
                self.send_json(404, {"error": "Log not found"})
                return
            body = log_path.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", len(body))
            self.end_headers()
            self.wfile.write(body)
            return

        # ── Browsable artifact UI ────────────────────────────────────────────
        # Routes: GET /  →  GET /artifacts/{repo}/  →  GET /artifacts/{repo}/{filename}
        # All sub-routes are namespaced under /artifacts/ to avoid any collision
        # with current or future API paths.

        # GET / — project index (entry point)
        if parsed.path in ('/', ''):
            repos = list_repos_with_artifacts()
            if repos:
                items = "".join(
                    f'<li><a href="/artifacts/{r}/">{r}</a></li>' for r in repos)
                listing = f"<ul>{items}</ul>"
            else:
                listing = "<p><em>No completed builds yet.</em></p>"
            self._send_html(200,
                f"<h1>Build Server</h1>{listing}"
                "<hr><p><small>Artifacts served live from the build workspace.</small></p>")
            return

        # GET /artifacts/{repo}/ — artifact list for one project
        m = re.match(r'^/artifacts/([A-Za-z0-9_-]+)/$', parsed.path)
        if m:
            repo = m.group(1)
            artifacts = find_artifacts(repo)
            if not artifacts:
                self._send_html(404,
                    f"<h1>{repo}</h1><p>No artifacts found.</p>"
                    '<p><a href="/">&larr; All projects</a></p>')
                return
            items = "".join(
                f'<li><a href="/artifacts/{repo}/{p.name}">{p.name}</a></li>'
                for p in artifacts
            )
            self._send_html(200,
                f"<h1>{repo}</h1>"
                '<p><a href="/">&larr; All projects</a></p>'
                f"<ul>{items}</ul>")
            return

        # GET /artifacts/{repo}/{filename} — file download
        m = re.match(r'^/artifacts/([A-Za-z0-9_-]+)/([^/]+)$', parsed.path)
        if m:
            repo = m.group(1)
            filename = m.group(2)
            # Validate: only labeled artifacts with known extensions
            if not re.match(r'^[A-Za-z0-9._-]+\.(apk|aab)$', filename):
                self.send_json(400, {"error": "Invalid filename"})
                return
            artifacts = find_artifacts(repo)
            target = next((p for p in artifacts if p.name == filename), None)
            if not target or not target.exists():
                self.send_json(404, {"error": "Artifact not found"})
                return
            ctype = ("application/vnd.android.package-archive"
                     if filename.endswith(".apk") else "application/octet-stream")
            size = target.stat().st_size
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", size)
            self.send_header("Content-Disposition",
                             f'attachment; filename="{filename}"')
            self.end_headers()
            with open(target, "rb") as f:
                while True:
                    chunk = f.read(65536)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
            return

        self.send_json(404, {"error": "Not found"})


# ── CLI mode ──────────────────────────────────────────────────────────────────

def run_cli(repo_dir, output_dir, force_clean):
    """Run a single build from a local repo directory and exit."""
    job_id = "cli"

    errors = check_system_dependencies()
    if errors:
        print("\nERROR: Missing system dependencies:\n", flush=True)
        for e in errors:
            print(e, flush=True)
            print(flush=True)
        sys.exit(1)

    WORK_DIR.mkdir(parents=True, exist_ok=True)

    manifest, error = load_manifest(repo_dir)
    if error:
        print(f"ERROR: {error}", flush=True)
        sys.exit(1)

    repo_name = repo_dir.name
    build_type = manifest["type"]
    node_version = manifest.get("node_version")

    print(f"Building {repo_name} ({build_type})...", flush=True)

    if build_type == "react-native":
        if not ensure_node(job_id, node_version):
            print("ERROR: Could not install Node.js.", flush=True)
            sys.exit(1)

        app_dir = rn_scaffold_dir(repo_name)
        clean = force_clean or needs_clean_build(repo_name, manifest) or not app_dir.exists()

        if clean:
            error = rn_init(repo_name, manifest, job_id)
            if error:
                print(f"ERROR: {error}", flush=True)
                sys.exit(1)

        error = rn_sync_source(repo_name, repo_dir, app_dir, job_id)
        if error:
            print(f"ERROR: {error}", flush=True)
            sys.exit(1)

        success, build_output = rn_build(repo_name, app_dir, job_id, node_version)

        if success:
            save_manifest_cache(repo_name, manifest)
            apk = app_dir / "android" / "app" / "build" / "outputs" / "apk" / "release" / "app-release.apk"
            signed_apk = app_dir / "android" / "app" / "build" / "outputs" / "apk" / "releaseSigned" / "app-releaseSigned.apk"
            aab = app_dir / "android" / "app" / "build" / "outputs" / "bundle" / "release" / "app-release.aab"
            artifacts = label_artifacts(manifest, repo_name, apk, signed_apk, aab)
        else:
            print(f"Build FAILED.\n{build_output}", flush=True)
            sys.exit(1)

    elif build_type == "android-native":
        app_dir = native_scaffold_dir(repo_name)
        clean = force_clean or needs_clean_build(repo_name, manifest) or not app_dir.exists()

        if clean:
            error = native_init(repo_name, manifest, job_id)
            if error:
                print(f"ERROR: {error}", flush=True)
                sys.exit(1)

        error = native_sync_source(repo_name, repo_dir, app_dir, job_id, manifest)
        if error:
            print(f"ERROR: {error}", flush=True)
            sys.exit(1)

        success, build_output = native_build(repo_name, app_dir, job_id)

        if success:
            save_manifest_cache(repo_name, manifest)
            apk = app_dir / "app" / "build" / "outputs" / "apk" / "release" / "app-release.apk"
            signed_apk = app_dir / "app" / "build" / "outputs" / "apk" / "releaseSigned" / "app-releaseSigned.apk"
            aab = app_dir / "app" / "build" / "outputs" / "bundle" / "release" / "app-release.aab"
            artifacts = label_artifacts(manifest, repo_name, apk, signed_apk, aab)
        else:
            print(f"Build FAILED.\n{build_output}", flush=True)
            sys.exit(1)

    else:
        print(f"ERROR: Unknown build type: {build_type}", flush=True)
        sys.exit(1)

    if output_dir:
        output_dir.mkdir(parents=True, exist_ok=True)
        for src in artifacts:
            shutil.copy2(src, output_dir / src.name)
        print(f"Build complete. Artifacts in {output_dir}", flush=True)
    else:
        print("Build complete.", flush=True)
        for p in artifacts:
            print(f"  {p}", flush=True)


# ── Startup ───────────────────────────────────────────────────────────────────

def run_server():
    print("Android Build Server starting up...", flush=True)

    errors = check_system_dependencies()
    if errors:
        print("\nERROR: Missing system dependencies:\n", flush=True)
        for e in errors:
            print(e, flush=True)
            print(flush=True)
        sys.exit(1)

    if not ensure_node():
        print("ERROR: Could not install Node.js. Exiting.", flush=True)
        sys.exit(1)

    for d in [WORK_DIR, CACHE_DIR, JOBS_DIR]:
        d.mkdir(parents=True, exist_ok=True)

    t = threading.Thread(target=worker, daemon=True)
    t.start()
    print("Build worker started.", flush=True)

    print(f"Build server listening on port {PORT}", flush=True)
    server = http.server.HTTPServer(("0.0.0.0", PORT), BuildHandler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("Shutting down.", flush=True)


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Android Build Server / Standalone CLI")
    parser.add_argument("--repo-dir", type=Path, metavar="DIR",
                        help="Path to repo root — enables standalone CLI mode")
    parser.add_argument("--output-dir", type=Path, metavar="DIR",
                        help="Directory to copy build artifacts into (CLI mode only)")
    parser.add_argument("--clean", action="store_true",
                        help="Force a clean scaffold rebuild")
    args = parser.parse_args()

    if args.repo_dir:
        run_cli(
            args.repo_dir.resolve(),
            args.output_dir.resolve() if args.output_dir else None,
            args.clean,
        )
    else:
        run_server()
