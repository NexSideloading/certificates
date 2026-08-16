#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

cleanup_zsign_artifacts() {
  local target_dir=$1
  rm -f "$target_dir"/zsign "$target_dir"/zsign.tgz "$target_dir"/zsign-linux-* "$target_dir"/*.tar.gz || true
  rm -rf "$target_dir"/zsign* || true
  rm -f "$target_dir"/NexStore.Info.plist "$target_dir"/NexStore.Info.plist.xml || true
}

normalize_release_version() {
  local raw_version=$1
  raw_version=${raw_version#v}
  printf '%s' "$raw_version"
}

version_is_older() {
  python3 - "$1" "$2" <<'PY'
import re
import sys


def parse_version(value: str) -> list[int]:
    numbers = [int(part) for part in re.findall(r"\d+", value)]
    return numbers or [0]


current = parse_version(sys.argv[1])
latest = parse_version(sys.argv[2])
limit = max(len(current), len(latest))
current.extend([0] * (limit - len(current)))
latest.extend([0] * (limit - len(latest)))

raise SystemExit(0 if tuple(current) < tuple(latest) else 1)
PY
}

download_latest_zsign() {
  local target_dir=$1
  local zsign_url

  echo "Resolving latest zsign release..."
  zsign_url="$(curl -fsSL https://api.github.com/repos/zhlynn/zsign/releases/latest \
    | jq -r '.assets[]? | select(.name == "zsign-linux-x86_64.tar.gz") | .browser_download_url' \
    | head -n 1)"

  if [[ -z "$zsign_url" || "$zsign_url" == "null" ]]; then
    echo "Failed to resolve latest zsign download URL" >&2
    return 1
  fi

  echo "Downloading zsign from: $zsign_url"
  wget -q --show-progress "$zsign_url" -O "$target_dir/zsign.tgz"

  echo "Extracting zsign..."
  tar -xzf "$target_dir/zsign.tgz" -C "$target_dir"

  chmod +x "$target_dir/zsign" || true

  if [[ ! -f "$target_dir/zsign" ]]; then
    echo "zsign binary not found after extraction" >&2
    return 1
  fi
}

cd "$REPO_ROOT"

BASE_RAW="${BASE_RAW_OVERRIDE:-}"
if [[ -z "$BASE_RAW" ]]; then
  if [[ -z "${GITHUB_REPOSITORY:-}" ]]; then
    echo "GITHUB_REPOSITORY is required when BASE_RAW_OVERRIDE is not set" >&2
    exit 1
  fi
  BASE_RAW="https://raw.githubusercontent.com/${GITHUB_REPOSITORY}/main"
fi

RELEASE_JSON=$(curl -fsSL https://api.github.com/repos/NovaDev404/NexStore/releases/latest)
IPA_URL=$(echo "$RELEASE_JSON" | jq -r '.assets[]? | select(.name | test("(?i)\\.ipa$")) | .browser_download_url' | head -n1)
VERSION_RAW=$(echo "$RELEASE_JSON" | jq -r '.tag_name // .name // empty')
LATEST_VERSION=$(normalize_release_version "$VERSION_RAW")

if [[ -z "$IPA_URL" || -z "$LATEST_VERSION" ]]; then
  echo "Failed to resolve the latest NexStore release metadata" >&2
  exit 1
fi

echo "Using release version: $LATEST_VERSION"
echo "IPA URL: $IPA_URL"

for d in */ ; do
  DIR=${d%/}
  if [[ "$DIR" == ".github" || "$DIR" == "scripts" ]]; then
    continue
  fi

  echo "Processing directory: $DIR"
  mkdir -p "$DIR/NexStore"

  shopt -s nullglob
  P12_FILES=("$DIR"/*.p12 "$DIR"/*.pfx)
  PROV_FILES=("$DIR"/*.mobileprovision)
  PASS_FILES=("$DIR"/password.txt "$DIR"/*.txt)
  EXISTING_IPAS=("$DIR"/NexStore/*.ipa)
  shopt -u nullglob

  P12=""
  PROV=""
  PASSFILE=""
  CERT_NAME=""

  if (( ${#P12_FILES[@]} > 0 )); then
    P12=$(realpath "${P12_FILES[0]}")
    CERT_NAME=$(basename "$P12")
    CERT_NAME=${CERT_NAME%.*}
  fi

  if (( ${#PROV_FILES[@]} > 0 )); then
    PROV=$(realpath "${PROV_FILES[0]}")
  fi

  if (( ${#PASS_FILES[@]} > 0 )); then
    PASSFILE=$(realpath "${PASS_FILES[0]}")
  fi

  if [[ -z "$CERT_NAME" ]]; then
    CERT_NAME="$DIR"
  fi

  EXISTING_IPA_NAMES=()
  NEEDS_VERSION_REFRESH=0

  if (( ${#EXISTING_IPAS[@]} > 0 )); then
    for existing_ipa in "${EXISTING_IPAS[@]}"; do
      ipa_name=$(basename "$existing_ipa")
      EXISTING_IPA_NAMES+=("$ipa_name")

      echo "Extracting Info.plist and XML from $DIR/NexStore/$ipa_name"
      if CURRENT_VERSION=$(python3 "$SCRIPT_DIR/extract_ipa_metadata.py" "$existing_ipa" "$DIR/NexStore"); then
        CURRENT_VERSION=$(printf '%s' "$CURRENT_VERSION" | tr -d '\r\n')
        if [[ -z "$CURRENT_VERSION" ]]; then
          echo "No version found inside $ipa_name, marking for refresh"
          NEEDS_VERSION_REFRESH=1
          continue
        fi

        echo "Found embedded version $CURRENT_VERSION in $ipa_name"
        if version_is_older "$CURRENT_VERSION" "$LATEST_VERSION"; then
          echo "Embedded version $CURRENT_VERSION is older than $LATEST_VERSION, marking for refresh"
          NEEDS_VERSION_REFRESH=1
        fi
      else
        echo "Failed to extract Info.plist from $ipa_name, marking for refresh"
        NEEDS_VERSION_REFRESH=1
      fi
    done
  else
    EXISTING_IPA_NAMES=("NexStore.ipa")
  fi

  if [[ -f "$DIR/NexStore/NexStore.ipa" && -f "$DIR/NexStore/install.plist" ]]; then
    echo "Existing build detected, validating injected signing assets: $DIR"

    if python3 "$SCRIPT_DIR/check_injected_signing_assets.py" "$DIR/NexStore/NexStore.ipa" "$CERT_NAME"; then
      if (( NEEDS_VERSION_REFRESH == 0 )); then
        echo "Already exists with injected signing assets and latest version, skipping: $DIR"
        cleanup_zsign_artifacts "$DIR/NexStore"
        continue
      fi

      echo "Existing IPA is outdated, rebuilding: $DIR"
    else
      echo "Existing IPA is missing injected signing assets, rebuilding: $DIR"
    fi

    if [[ -z "$P12" || -z "$PROV" || -z "$PASSFILE" ]]; then
      echo "Existing IPA requires rebuild for $DIR, but signing files are unavailable, leaving current build untouched"
      cleanup_zsign_artifacts "$DIR/NexStore"
      continue
    fi

    rm -f "$DIR/NexStore/NexStore.ipa" "$DIR/NexStore/install.plist"
    for ipa_name in "${EXISTING_IPA_NAMES[@]}"; do
      if [[ "$ipa_name" != "NexStore.ipa" ]]; then
        rm -f "$DIR/NexStore/$ipa_name"
      fi
    done
  fi

  cd "$DIR/NexStore"

  echo "Downloading NexStore.ipa..."
  wget -q --show-progress "$IPA_URL" -O NexStore.ipa

  echo "Downloading zsign..."
  download_latest_zsign "."

  if ! ldd ./zsign >/dev/null 2>&1; then
    echo "ldd for zsign:"
    ldd ./zsign || true
  fi

  if [[ -z "$P12" || -z "$PROV" || -z "$PASSFILE" ]]; then
    echo "Missing signing files (.p12/.pfx, .mobileprovision, password.txt) for $DIR, skipping"
    cleanup_zsign_artifacts "."
    cd ../..
    continue
  fi

  PASSWORD=$(tr -d '\r\n' < "$PASSFILE")

  echo "Signing NexStore.ipa..."
  if ! ./zsign -k "$P12" -m "$PROV" -p "$PASSWORD" -o NexStore-signed.ipa NexStore.ipa ; then
    echo "zsign failed for $DIR"
    ldd ./zsign || true
    cleanup_zsign_artifacts "."
    cd ../..
    continue
  fi

  echo "Injecting signing assets into signed IPA..."
  INJECT_DIR=$(mktemp -d)
  if ! unzip -q NexStore-signed.ipa -d "$INJECT_DIR"; then
    echo "Failed to unpack signed IPA for $DIR"
    rm -rf "$INJECT_DIR"
    cleanup_zsign_artifacts "."
    cd ../..
    continue
  fi

  APP_DIR=$(find "$INJECT_DIR/Payload" -maxdepth 1 -type d -name '*.app' | head -n1)
  if [[ -z "$APP_DIR" ]]; then
    echo "Failed to locate .app bundle inside signed IPA for $DIR"
    rm -rf "$INJECT_DIR"
    cleanup_zsign_artifacts "."
    cd ../..
    continue
  fi

  ASSET_DIR="$APP_DIR/signing-assets/$CERT_NAME"
  mkdir -p "$ASSET_DIR"
  cp "$P12" "$ASSET_DIR/cert.p12"
  cp "$PROV" "$ASSET_DIR/cert.mobileprovision"
  cp "$PASSFILE" "$ASSET_DIR/cert.txt"

  INJECTED_IPA="NexStore-injected.ipa"
  (
    cd "$INJECT_DIR"
    zip -qry "$OLDPWD/$INJECTED_IPA" Payload
  )

  echo "Re-signing injected IPA so bundle contents stay valid..."
  if ! ./zsign -k "$P12" -m "$PROV" -p "$PASSWORD" -o NexStore-final.ipa "$INJECTED_IPA" ; then
    echo "zsign failed after injecting assets for $DIR"
    ldd ./zsign || true
    rm -rf "$INJECT_DIR"
    rm -f "$INJECTED_IPA" NexStore-signed.ipa
    cleanup_zsign_artifacts "."
    cd ../..
    continue
  fi

  rm -rf "$INJECT_DIR"
  rm -f "$INJECTED_IPA" NexStore-signed.ipa
  rm -f NexStore.ipa
  mv NexStore-final.ipa NexStore.ipa

  for ipa_name in "${EXISTING_IPA_NAMES[@]}"; do
    if [[ "$ipa_name" == "NexStore.ipa" ]]; then
      continue
    fi

    cp NexStore.ipa "$ipa_name"
  done

  export IPA_LINK="${BASE_RAW}/${DIR}/NexStore/NexStore.ipa"
  export NS_VERSION="$LATEST_VERSION"

  echo "Writing install.plist using scripts/write_plist.py..."
  python3 ../../scripts/write_plist.py

  echo "Extracting Info.plist and XML from refreshed IPAs..."
  for ipa_name in "${EXISTING_IPA_NAMES[@]}"; do
    python3 ../../scripts/extract_ipa_metadata.py "$ipa_name" . >/dev/null
  done

  echo "Cleaning up zsign artifacts in $DIR/NexStore ..."
  cleanup_zsign_artifacts "."

  cd ../..
done
