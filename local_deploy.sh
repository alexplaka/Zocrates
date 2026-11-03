#!/usr/bin/env bash
# local_deploy.sh — Build & install the Zocrates extension
# Usage: ./local_deploy.sh --profile <name> [--restart]
#    or: ./local_deploy.sh <name> [--restart]
set -euo pipefail

RESTART=0
PROFILE_NAME=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --restart)
      RESTART=1
      shift
      ;;
    --profile|-P)
      if [[ -n "${2:-}" ]]; then
        PROFILE_NAME="$2"
        shift 2
      else
        echo "❌ Error: $1 requires a profile name argument." >&2
        echo "Usage: ./local_deploy.sh --profile <profile-name> [--restart]" >&2
        echo "   or: ./local_deploy.sh <profile-name> [--restart]" >&2
        exit 1
      fi
      ;;
    --help|-h)
      echo "local_deploy.sh — Build & install the Zocrates extension"
      echo "Usage: ./local_deploy.sh --profile <profile-name> [--restart]"
      echo "   or: ./local_deploy.sh <profile-name> [--restart]"
      echo ""
      echo "Options:"
      echo "  --profile, -P <name>  Target Zotero profile name (required)"
      echo "  --restart             Restart Zotero after deployment"
      echo "  --help, -h            Show this help message"
      exit 0
      ;;
    -*)
      echo "❌ Unknown option: $1" >&2
      echo "Usage: ./local_deploy.sh --profile <profile-name> [--restart]" >&2
      echo "   or: ./local_deploy.sh <profile-name> [--restart]" >&2
      exit 1
      ;;
    *)
      PROFILE_NAME="$1"
      shift
      ;;
  esac
done

# Detect OS/platform
PLATFORM="linux"
case "$(uname -s)" in
  Darwin*)
    PLATFORM="macos"
    ;;
  Linux*)
    PLATFORM="linux"
    ;;
  MINGW*|MSYS*|CYGWIN*|Windows_NT)
    PLATFORM="windows"
    ;;
  *)
    if [[ "${OSTYPE:-}" == darwin* ]]; then
      PLATFORM="macos"
    elif [[ "${OSTYPE:-}" == linux* ]]; then
      PLATFORM="linux"
    elif [[ "${OSTYPE:-}" == msys* || "${OSTYPE:-}" == cygwin* || "${OSTYPE:-}" == win32* ]]; then
      PLATFORM="windows"
    fi
    ;;
esac

# Candidate app directories across platforms
CANDIDATE_DIRS=()
case "$PLATFORM" in
  macos)
    CANDIDATE_DIRS=(
      "$HOME/Library/Application Support/Zotero"
    )
    ;;
  linux)
    CANDIDATE_DIRS=(
      "$HOME/.zotero/zotero"
      "$HOME/.var/app/org.zotero.Zotero/.zotero/zotero"
      "$HOME/snap/zotero/current/.zotero/zotero"
      "$HOME/snap/zotero/common/.zotero/zotero"
    )
    ;;
  windows)
    if [[ -n "${APPDATA:-}" ]]; then
      CANDIDATE_DIRS+=("$APPDATA/Zotero/Zotero")
    fi
    CANDIDATE_DIRS+=(
      "$HOME/AppData/Roaming/Zotero/Zotero"
    )
    ;;
esac

PYTHON_PROFILE_HELPER='
import sys, os, glob, configparser

def get_profiles(candidate_dirs):
    discovered = []
    seen_paths = set()
    for base in candidate_dirs:
        if not base:
            continue
        base = os.path.expanduser(base)
        if not os.path.isdir(base):
            continue
        ini = os.path.join(base, "profiles.ini")
        if os.path.isfile(ini):
            cp = configparser.RawConfigParser()
            try:
                with open(ini, "r", encoding="utf-8", errors="replace") as f:
                    cp.read_file(f)
                for sec in cp.sections():
                    if sec.startswith("Profile") or cp.has_option(sec, "Name"):
                        name = cp.get(sec, "Name", fallback=None)
                        path = cp.get(sec, "Path", fallback=None)
                        is_rel = cp.get(sec, "IsRelative", fallback="1") == "1"
                        if name and path:
                            p = os.path.normpath(os.path.join(base, path) if is_rel else path)
                            if os.path.isdir(p) and p not in seen_paths:
                                discovered.append({"name": name, "path": p, "base": base})
                                seen_paths.add(p)
            except Exception:
                pass
        for pattern in [os.path.join(base, "*.*"), os.path.join(base, "Profiles", "*.*")]:
            for p in sorted(glob.glob(pattern)):
                if os.path.isdir(p) and p not in seen_paths:
                    base_name = os.path.basename(p)
                    name = base_name.split(".", 1)[1] if "." in base_name else base_name
                    discovered.append({"name": name, "path": os.path.normpath(p), "base": base})
                    seen_paths.add(p)
    return discovered

action = sys.argv[1] if len(sys.argv) > 1 else "list"

if action == "list":
    candidates = sys.argv[2:]
    profiles = get_profiles(candidates)
    if profiles:
        print("Available profiles across detected Zotero installations:", file=sys.stderr)
        for prof in profiles:
            name, path = prof["name"], prof["path"]
            print(f"  - {name} ({path})", file=sys.stderr)
    else:
        print("No Zotero profiles found in candidate directories:", file=sys.stderr)
        for c in candidates:
            if c:
                print(f"  - {c}", file=sys.stderr)
    sys.exit(0)

elif action == "resolve":
    target = sys.argv[2]
    candidates = sys.argv[3:]
    profiles = get_profiles(candidates)

    resolved_path = None
    canonical_name = target

    exp = os.path.abspath(os.path.expanduser(target))
    if os.path.isdir(exp):
        resolved_path = exp
        canonical_name = os.path.basename(exp)
    else:
        for prof in profiles:
            if prof["name"] == target:
                resolved_path = prof["path"]
                canonical_name = prof["name"]
                break
        if not resolved_path:
            for prof in profiles:
                if os.path.basename(prof["path"]) == target:
                    resolved_path = prof["path"]
                    canonical_name = prof["name"]
                    break
        if not resolved_path:
            for prof in profiles:
                if os.path.basename(prof["path"]).endswith("." + target):
                    resolved_path = prof["path"]
                    canonical_name = prof["name"]
                    break
        if not resolved_path:
            for prof in profiles:
                if prof["name"].lower() == target.lower():
                    resolved_path = prof["path"]
                    canonical_name = prof["name"]
                    break
        if not resolved_path:
            for prof in profiles:
                if os.path.basename(prof["path"]).lower().endswith("." + target.lower()):
                    resolved_path = prof["path"]
                    canonical_name = prof["name"]
                    break

    if resolved_path and os.path.isdir(resolved_path):
        print(resolved_path)
        print(canonical_name)
        sys.exit(0)
    else:
        print(f"❌ Error: Could not find Zotero profile directory for \"{target}\".", file=sys.stderr)
        if profiles:
            print("Available profiles across detected Zotero installations:", file=sys.stderr)
            for prof in profiles:
                name, path = prof["name"], prof["path"]
                print(f"  - {name} ({path})", file=sys.stderr)
        else:
            print("Candidate directories searched:", file=sys.stderr)
            for c in candidates:
                if c:
                    print(f"  - {c}", file=sys.stderr)
        sys.exit(1)
'

if [[ -z "$PROFILE_NAME" ]]; then
  echo "❌ Error: Target Zotero profile name is required." >&2
  echo "Usage: ./local_deploy.sh --profile <profile-name> [--restart]" >&2
  echo "   or: ./local_deploy.sh <profile-name> [--restart]" >&2
  echo "" >&2
  python3 -c "$PYTHON_PROFILE_HELPER" list "${CANDIDATE_DIRS[@]}"
  exit 1
fi

RESOLVE_OUT=$(python3 -c "$PYTHON_PROFILE_HELPER" resolve "$PROFILE_NAME" "${CANDIDATE_DIRS[@]}") || exit 1
PROFILE_DIR=$(echo "$RESOLVE_OUT" | head -n1)
RESOLVED_NAME=$(echo "$RESOLVE_OUT" | sed -n '2p')
if [[ -n "$RESOLVED_NAME" ]]; then
  PROFILE_NAME="$RESOLVED_NAME"
fi

ADDON_DIR="zotero-plugin/addon"
XPI="zocrates.xpi"

# Dynamically extract the ID from manifest.json to match Gecko packaging rules
EXT_ID=$(grep -o '"id": *"[^"]*"' "$ADDON_DIR/manifest.json" | head -n1 | cut -d'"' -f4)

RUNNING_FLAVOR=""
if [[ "$RESTART" -eq 1 ]]; then
  # 1. Detect currently running flavor before stopping
  if [[ "$PLATFORM" == "macos" ]]; then
    if pgrep -f "Zotero.app" >/dev/null 2>&1 || pgrep -x zotero >/dev/null 2>&1 || pgrep -x zotero-bin >/dev/null 2>&1; then
      RUNNING_FLAVOR="macos"
    fi
  elif [[ "$PLATFORM" == "linux" ]]; then
    if (command -v flatpak >/dev/null 2>&1 && flatpak ps 2>/dev/null | grep -q "org.zotero.Zotero") || pgrep -f "org.zotero.Zotero" >/dev/null 2>&1 || pgrep -f "/app/share/zotero" >/dev/null 2>&1; then
      RUNNING_FLAVOR="flatpak"
    elif pgrep -f "/snap/zotero" >/dev/null 2>&1; then
      RUNNING_FLAVOR="snap"
    elif pgrep -x zotero >/dev/null 2>&1 || pgrep -x zotero-bin >/dev/null 2>&1; then
      RUNNING_FLAVOR="native"
    fi
  elif [[ "$PLATFORM" == "windows" ]]; then
    if tasklist 2>/dev/null | grep -qi "zotero.exe"; then
      RUNNING_FLAVOR="windows"
    fi
  fi

  # 2. Stop running backend
  echo "🛑 Stopping running backend..."
  pkill -f "zocrates.fast_api_app" 2>/dev/null || true
  # Windows fallback for stopping backend via backend.pid or taskkill
  if [[ -f "$PROFILE_DIR/zocrates-data/backend.pid" ]]; then
    taskkill //F //PID "$(cat "$PROFILE_DIR/zocrates-data/backend.pid" 2>/dev/null)" 2>/dev/null || true
  fi

  # 3. Stop Zotero
  if [[ "$PLATFORM" == "macos" ]]; then
    echo "🛑 Stopping Zotero (macOS)..."
    pkill -x zotero 2>/dev/null || true
    pkill -f "Zotero.app" 2>/dev/null || true
  elif [[ "$PLATFORM" == "windows" ]]; then
    echo "🛑 Stopping Zotero (Windows)..."
    taskkill //F //IM zotero.exe 2>/dev/null || true
  elif [[ "$PLATFORM" == "linux" ]]; then
    if [[ "$RUNNING_FLAVOR" == "flatpak" ]]; then
      echo "🛑 Stopping Zotero Flatpak..."
      flatpak kill org.zotero.Zotero 2>/dev/null || true
    elif [[ "$RUNNING_FLAVOR" == "snap" ]]; then
      echo "🛑 Stopping Zotero Snap..."
      pkill -f "/snap/zotero" 2>/dev/null || true
    elif [[ "$RUNNING_FLAVOR" == "native" ]]; then
      echo "🛑 Stopping Native Zotero..."
      pkill -x zotero 2>/dev/null || pkill -x zotero-bin 2>/dev/null || true
    else
      flatpak kill org.zotero.Zotero 2>/dev/null || true
      pkill -f "/snap/zotero" 2>/dev/null || true
      pkill -x zotero 2>/dev/null || true
      pkill -x zotero-bin 2>/dev/null || true
    fi
  fi
  sleep 1.5

  # 4. Clear startup and chrome script caches for native, Flatpak, Snap, macOS, Windows
  echo "🧹 Clearing startup and chrome script caches..."
  # Profile-specific caches
  rm -rf "$PROFILE_DIR/startupCache" 2>/dev/null || true
  rm -rf "$PROFILE_DIR/storage/default/chrome/cache" 2>/dev/null || true
  # Native Linux
  rm -rf "$HOME/.cache/zotero/zotero"/*/startupCache 2>/dev/null || true
  rm -rf "$HOME/.zotero/zotero"/*/startupCache 2>/dev/null || true
  rm -rf "$HOME/.zotero/zotero"/*/storage/default/chrome/cache 2>/dev/null || true
  # Flatpak Linux
  rm -rf "$HOME/.var/app/org.zotero.Zotero/cache/zotero/zotero"/*/startupCache 2>/dev/null || true
  rm -rf "$HOME/.var/app/org.zotero.Zotero/.zotero/zotero"/*/startupCache 2>/dev/null || true
  rm -rf "$HOME/.var/app/org.zotero.Zotero/.zotero/zotero"/*/storage/default/chrome/cache 2>/dev/null || true
  # Snap Linux
  rm -rf "$HOME/snap/zotero/common/.cache/zotero/zotero"/*/startupCache 2>/dev/null || true
  rm -rf "$HOME/snap/zotero/current/.cache/zotero/zotero"/*/startupCache 2>/dev/null || true
  rm -rf "$HOME/snap/zotero/common/.zotero/zotero"/*/startupCache 2>/dev/null || true
  rm -rf "$HOME/snap/zotero/current/.zotero/zotero"/*/startupCache 2>/dev/null || true
  rm -rf "$HOME/snap/zotero/common/.zotero/zotero"/*/storage/default/chrome/cache 2>/dev/null || true
  rm -rf "$HOME/snap/zotero/current/.zotero/zotero"/*/storage/default/chrome/cache 2>/dev/null || true
  # macOS
  rm -rf "$HOME/Library/Caches/Zotero"/*/startupCache 2>/dev/null || true
  rm -rf "$HOME/Library/Caches/Zotero/Profiles"/*/startupCache 2>/dev/null || true
  rm -rf "$HOME/Library/Application Support/Zotero/Profiles"/*/startupCache 2>/dev/null || true
  rm -rf "$HOME/Library/Application Support/Zotero"/*/startupCache 2>/dev/null || true
  rm -rf "$HOME/Library/Application Support/Zotero/Profiles"/*/storage/default/chrome/cache 2>/dev/null || true
  rm -rf "$HOME/Library/Application Support/Zotero"/*/storage/default/chrome/cache 2>/dev/null || true
  # Windows
  if [[ -n "${LOCALAPPDATA:-}" ]]; then
    rm -rf "$LOCALAPPDATA/Zotero/Zotero"/*/startupCache 2>/dev/null || true
    rm -rf "$LOCALAPPDATA/Zotero/Zotero/Profiles"/*/startupCache 2>/dev/null || true
    rm -rf "$LOCALAPPDATA/Zotero/Zotero/Profiles"/*/storage/default/chrome/cache 2>/dev/null || true
  fi
  rm -rf "$HOME/AppData/Local/Zotero/Zotero"/*/startupCache 2>/dev/null || true
  rm -rf "$HOME/AppData/Local/Zotero/Zotero/Profiles"/*/startupCache 2>/dev/null || true
  rm -rf "$HOME/AppData/Local/Zotero/Zotero/Profiles"/*/storage/default/chrome/cache 2>/dev/null || true
fi

echo "🔄 Synchronizing version from pyproject.toml to manifest.json..."
python3 -c '
import json, tomllib
from pathlib import Path

pyproj = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))
ver = pyproj["project"]["version"]
mpath = Path("'"$ADDON_DIR"'/manifest.json")
mdata = json.loads(mpath.read_text(encoding="utf-8"))
if mdata.get("version") != ver:
    mdata["version"] = ver
    mpath.write_text(json.dumps(mdata, indent=2) + "\n", encoding="utf-8")
    print(f"   Synced manifest.json version to {ver}")
else:
    print(f"   manifest.json already in sync with pyproject.toml ({ver})")
'

echo "📦 Building backend wheel..."
rm -f build_dist/*.whl  # clear stale wheels so the glob below always matches exactly one fresh build
uv build --wheel --out-dir build_dist
WHEEL_FILE=$(ls build_dist/zocrates-*.whl | head -n1)
echo "Backend wheel built: $WHEEL_FILE"
# bootstrap.js derives the versioned wheel install target (zocrates-<version>-py3-none-any.whl)
# from manifest.json, so the wheel version must match the extension version.
WHEEL_VERSION=$(basename "$WHEEL_FILE" | sed -E 's/^zocrates-([^-]+)-[^-]+-[^-]+-[^-]+\.whl$/\1/')
MANIFEST_VERSION=$(grep -o '"version": *"[^"]*"' "$ADDON_DIR/manifest.json" | head -n1 | cut -d'"' -f4)
if [[ "$WHEEL_VERSION" != "$MANIFEST_VERSION" ]]; then
  echo "❌ Version mismatch: built wheel '$WHEEL_FILE' (v$WHEEL_VERSION) does not match '$ADDON_DIR/manifest.json' (v$MANIFEST_VERSION)." >&2
  echo "   Keep pyproject.toml version and manifest.json version in sync so bootstrap.js can install the versioned wheel target." >&2
  exit 1
fi
mkdir -p "$ADDON_DIR/backend"
cp build_dist/zocrates-*.whl "$ADDON_DIR/backend/zocrates.whl"
# Phase 6 pinned provisioning: export the lockfile-derived constraint matrix
# alongside the wheel so user venvs install the exact tested dependency pins
# (uv pip install --constraint) instead of resolving the wheel's open ranges
# against current PyPI at install time. A failed export aborts the deploy
# under set -euo pipefail.
uv export --locked --format requirements-txt --no-dev --no-emit-project --no-hashes --output-file "$ADDON_DIR/backend/constraints.txt"
echo "📌 Pinned constraint matrix: $(grep -c "==" "$ADDON_DIR/backend/constraints.txt") exact pins staged at backend/constraints.txt"

echo "📦 Building $XPI..."
rm -f "$XPI"
(cd "$ADDON_DIR" && zip -r "../../$XPI" .)

echo "🚀 Copying to Zotero profile '$PROFILE_NAME' ($PROFILE_DIR)..."
mkdir -p "$PROFILE_DIR/extensions"
cp "$XPI" "$PROFILE_DIR/extensions/$EXT_ID.xpi"

# Invalidate the local backend version marker so redeploying without a version bump
# still triggers an incremental wheel install into .venv on the next launch.
VERSION_MARKER_CANDIDATES=(
  "$PROFILE_DIR/zocrates-data"
  "$HOME/Zotero/$PROFILE_NAME/zocrates-data"
  "$HOME/Zotero/Zotero $PROFILE_NAME/zocrates-data"
  "$HOME/Zotero $PROFILE_NAME/zocrates-data"
  "$HOME/Zotero/zocrates-data"
  "$HOME/Documents/Zotero/zocrates-data"
  "$HOME/Documents/Zotero/$PROFILE_NAME/zocrates-data"
  "$HOME/Documents/Zotero $PROFILE_NAME/zocrates-data"
  "$HOME/Documents/Zotero/Zotero $PROFILE_NAME/zocrates-data"
  "$HOME/snap/zotero/common/Zotero/$PROFILE_NAME/zocrates-data"
  "$HOME/snap/zotero/common/Zotero/Zotero $PROFILE_NAME/zocrates-data"
  "$HOME/snap/zotero/common/Zotero $PROFILE_NAME/zocrates-data"
  "$HOME/snap/zotero/common/Zotero/zocrates-data"
  "$HOME/snap/zotero/current/Zotero/$PROFILE_NAME/zocrates-data"
  "$HOME/snap/zotero/current/Zotero/Zotero $PROFILE_NAME/zocrates-data"
  "$HOME/snap/zotero/current/Zotero $PROFILE_NAME/zocrates-data"
  "$HOME/snap/zotero/current/Zotero/zocrates-data"
  "$HOME/.var/app/org.zotero.Zotero/data/zotero/$PROFILE_NAME/zocrates-data"
  "$HOME/.var/app/org.zotero.Zotero/data/zotero/Zotero $PROFILE_NAME/zocrates-data"
  "$HOME/.var/app/org.zotero.Zotero/data/zotero/zocrates-data"
)

for data_candidate in "${VERSION_MARKER_CANDIDATES[@]}"; do
  if [[ -f "$data_candidate/.venv/.zocrates-version" ]]; then
    rm -f "$data_candidate/.venv/.zocrates-version"
    echo "🧹 Cleared local backend version marker in $data_candidate"
  fi
done

for globbed in \
  "$HOME"/Zotero*/zocrates-data \
  "$HOME"/Zotero*/*/zocrates-data \
  "$HOME"/Documents/Zotero*/zocrates-data \
  "$HOME"/Documents/Zotero*/*/zocrates-data; do
  if [[ -f "$globbed/.venv/.zocrates-version" ]]; then
    rm -f "$globbed/.venv/.zocrates-version"
    echo "🧹 Cleared local backend version marker in $globbed"
  fi
done

echo "✅ Done."

if [[ "$RESTART" -eq 1 ]]; then
  if [[ "$PLATFORM" == "macos" ]]; then
    echo "🔄 Starting Zotero (macOS) with profile '$PROFILE_NAME'..."
    if [[ -d "/Applications/Zotero.app" ]]; then
      open -a /Applications/Zotero.app --args -P "$PROFILE_NAME"
    elif command -v zotero >/dev/null 2>&1; then
      nohup zotero -P "$PROFILE_NAME" > /tmp/zotero.log 2>&1 &
    else
      open -a Zotero --args -P "$PROFILE_NAME"
    fi
    echo "✅ Zotero restarted ($PROFILE_NAME)."
  elif [[ "$PLATFORM" == "linux" ]]; then
    TARGET_FLAVOR="$RUNNING_FLAVOR"
    if [[ -z "$TARGET_FLAVOR" ]]; then
      if command -v zotero >/dev/null 2>&1; then
        TARGET_FLAVOR="native"
      elif command -v flatpak >/dev/null 2>&1 && flatpak info org.zotero.Zotero >/dev/null 2>&1; then
        TARGET_FLAVOR="flatpak"
      elif command -v snap >/dev/null 2>&1 && snap list zotero >/dev/null 2>&1; then
        TARGET_FLAVOR="snap"
      else
        TARGET_FLAVOR="native"
      fi
    fi

    if [[ "$TARGET_FLAVOR" == "snap" ]]; then
      echo "🔄 Starting Zotero Snap with profile '$PROFILE_NAME'..."
      nohup snap run zotero -P "$PROFILE_NAME" > /tmp/zotero_snap.log 2>&1 &
      echo "✅ Zotero Snap restarted ($PROFILE_NAME)."
    elif [[ "$TARGET_FLAVOR" == "flatpak" ]]; then
      echo "🔄 Starting Zotero Flatpak with profile '$PROFILE_NAME'..."
      nohup flatpak run org.zotero.Zotero -P "$PROFILE_NAME" > /tmp/zotero_flatpak.log 2>&1 &
      echo "✅ Zotero Flatpak restarted ($PROFILE_NAME)."
    elif command -v zotero >/dev/null 2>&1; then
      echo "🔄 Starting Native Zotero with profile '$PROFILE_NAME'..."
      nohup zotero -P "$PROFILE_NAME" > /tmp/zotero.log 2>&1 &
      echo "✅ Native Zotero restarted ($PROFILE_NAME)."
    else
      echo "⚠️ Notice: Could not find 'zotero' binary on PATH to launch automatically."
      echo "   Please start Zotero manually with: zotero -P \"$PROFILE_NAME\""
    fi
  elif [[ "$PLATFORM" == "windows" ]]; then
    echo "🔄 Starting Zotero (Windows) with profile '$PROFILE_NAME'..."
    if [[ -f "/c/Program Files/Zotero/zotero.exe" ]]; then
      nohup "/c/Program Files/Zotero/zotero.exe" -P "$PROFILE_NAME" > /tmp/zotero.log 2>&1 &
    elif [[ -f "C:\\Program Files\\Zotero\\zotero.exe" ]]; then
      "C:\\Program Files\\Zotero\\zotero.exe" -P "$PROFILE_NAME" &
    elif command -v zotero >/dev/null 2>&1; then
      nohup zotero -P "$PROFILE_NAME" > /tmp/zotero.log 2>&1 &
    elif [[ -f "C:\\Program Files (x86)\\Zotero\\zotero.exe" ]]; then
      "C:\\Program Files (x86)\\Zotero\\zotero.exe" -P "$PROFILE_NAME" &
    else
      nohup zotero.exe -P "$PROFILE_NAME" > /tmp/zotero.log 2>&1 &
    fi
    echo "✅ Zotero restarted ($PROFILE_NAME)."
  fi
fi
