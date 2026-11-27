#!/usr/bin/env bash
# SpArk setup: configure, build, flash and install SpArkNode, SpArkKids and SpArk Room Bridge.
#
#   ./setup.sh                 interactive install (macOS or Raspberry Pi OS)
#   ./setup.sh --dry-run       ask everything, write .env.dry-run, print every command instead of
#                              running it (no builds, no flashing, no installs)
#   ./setup.sh --defaults      take the default for every question (combine with --dry-run to
#                              see the whole flow without typing)
#   ./setup.sh --env FILE      use FILE instead of .env
#
# Answers go to .env (gitignored, chmod 600). Re-running offers the current values as defaults.
# Works with the bash 3.2 that ships with macOS.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
EXAMPLE="$ROOT/.env.example"
ENV_FILE=""
DRY_RUN=0
DEFAULTS=0
NODE_ENV="SparkNode_P1"
KIDS_ENV="SparkKids_TDeckPlus"
TOOLS="$ROOT/.spark-tools"

while [ $# -gt 0 ]; do
  case "$1" in
    --dry-run) DRY_RUN=1 ;;
    --defaults) DEFAULTS=1 ;;
    --env) shift; ENV_FILE="${1:?--env needs a file}" ;;
    -h|--help) sed -n '2,13p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown option: $1 (see --help)" >&2; exit 2 ;;
  esac
  shift
done
if [ -z "$ENV_FILE" ]; then
  if [ "$DRY_RUN" = 1 ]; then ENV_FILE="$ROOT/.env.dry-run"; else ENV_FILE="$ROOT/.env"; fi
fi
case "$ENV_FILE" in /*) ;; *) ENV_FILE="$PWD/$ENV_FILE" ;; esac

OS="$(uname -s)"
case "$OS" in
  Darwin) PLATFORM=mac ;;
  Linux) PLATFORM=linux ;;
  *) echo "Unsupported system: $OS (macOS and Raspberry Pi OS are supported)" >&2; exit 1 ;;
esac

# ---------------------------------------------------------------------------------------------
# output helpers

if [ -t 1 ]; then B=$'\033[1m'; D=$'\033[2m'; G=$'\033[32m'; Y=$'\033[33m'; R=$'\033[31m'; N=$'\033[0m'
else B=""; D=""; G=""; Y=""; R=""; N=""; fi
title() { printf '\n%s== %s ==%s\n' "$B" "$1" "$N"; }
info() { printf '%s\n' "$*"; }
ok() { printf '%s%s%s\n' "$G" "$*" "$N"; }
warn() { printf '%s%s%s\n' "$Y" "$*" "$N" >&2; }
die() { printf '%s%s%s\n' "$R" "$*" "$N" >&2; exit 1; }

# run a command, or only show it in dry-run mode
run() {
  if [ "$DRY_RUN" = 1 ]; then printf '%s  + %s%s\n' "$D" "$*" "$N"; return 0; fi
  "$@"
}
# like run, for a command line that needs a shell (pipes, redirects)
run_sh() {
  if [ "$DRY_RUN" = 1 ]; then printf '%s  + %s%s\n' "$D" "$1" "$N"; return 0; fi
  bash -c "$1"
}

# ---------------------------------------------------------------------------------------------
# questions

# ask VAR "Question" default  -> sets VAR
ask() {
  local __var="$1" __q="$2" __def="${3-}" __ans=""
  if [ "$DEFAULTS" = 1 ]; then printf '%s [%s]: %s(default)%s\n' "$__q" "$__def" "$D" "$N"; printf -v "$__var" '%s' "$__def"; return; fi
  read -r -p "$__q [$__def]: " __ans || true
  printf -v "$__var" '%s' "${__ans:-$__def}"
}

# ask_secret VAR "Question" current generated -> never echoes; empty answer keeps current (or the
# generated proposal, which is shown once so it can be written down)
ask_secret() {
  local __var="$1" __q="$2" __cur="${3-}" __gen="${4-}" __ans="" __hint
  if [ -n "$__cur" ]; then __hint="Enter = keep current"
  elif [ -n "$__gen" ]; then __hint="Enter = use $__gen"
  else __hint="Enter = leave empty"; fi
  if [ "$DEFAULTS" = 1 ]; then printf '%s (%s): %s(default)%s\n' "$__q" "$__hint" "$D" "$N"
  else read -r -s -p "$__q ($__hint): " __ans || true; echo; fi
  if [ -z "$__ans" ]; then __ans="${__cur:-$__gen}"; fi
  printf -v "$__var" '%s' "$__ans"
}

# yesno "Question" y|n -> exit status
yesno() {
  local __def="$2" __ans=""
  if [ "$DEFAULTS" = 1 ]; then printf '%s [%s]: %s(default)%s\n' "$1" "$__def" "$D" "$N"; __ans="$__def"
  else read -r -p "$1 [$( [ "$__def" = y ] && echo Y/n || echo y/N )]: " __ans || true; __ans="${__ans:-$__def}"; fi
  case "$__ans" in [Yy]*) return 0 ;; *) return 1 ;; esac
}

pause() {
  if [ "$DRY_RUN" = 1 ] || [ "$DEFAULTS" = 1 ]; then printf '%s(%s)%s\n' "$D" "$1" "$N"; return; fi
  read -r -p "$1 Press Enter when ready... " _ || true
}

rand_from() {  # rand_from <tr set> <length>
  (set +o pipefail; LC_ALL=C tr -dc "$1" </dev/urandom 2>/dev/null | head -c "$2")
}

valid_text() {  # no quotes, backslashes, backticks or $ (they would break firmware build flags)
  case "$1" in *\"*|*\'*|*\\*|*\`*|*\$*) return 1 ;; esac
  return 0
}

ask_text() {  # ask_text VAR "Question" default maxlen
  local __v
  while :; do
    ask __v "$2" "$3"
    if ! valid_text "$__v"; then warn "Quotes, backslashes, backticks and \$ are not allowed."; [ "$DEFAULTS" = 1 ] && die "invalid default for $1"; continue; fi
    if [ "${#__v}" -gt "$4" ]; then warn "At most $4 characters."; [ "$DEFAULTS" = 1 ] && die "invalid default for $1"; continue; fi
    break
  done
  printf -v "$1" '%s' "$__v"
}

ask_match() {  # ask_match VAR "Question" default regex "error"
  local __v
  while :; do
    ask __v "$2" "$3"
    if printf '%s\n' "$__v" | grep -Eq "$4"; then break; fi
    warn "$5"; [ "$DEFAULTS" = 1 ] && die "invalid default for $1"
  done
  printf -v "$1" '%s' "$__v"
}

# ---------------------------------------------------------------------------------------------
# time zone: one IANA name (bridge) and its POSIX TZ string (firmware)

system_tz() {
  local z=""
  if [ -f /etc/timezone ]; then z="$(head -n 1 /etc/timezone)"; fi
  if [ -z "$z" ] && [ -L /etc/localtime ]; then z="$(readlink /etc/localtime | sed 's|.*zoneinfo/||')"; fi
  printf '%s' "${z:-UTC}"
}
posix_tz() {  # posix_tz Area/City -> the POSIX TZ string in the footer of its TZif file
  local f="/usr/share/zoneinfo/$1" l
  [ -f "$f" ] || return 1
  l="$(tail -n 1 "$f" 2>/dev/null | tr -d '\r')"
  printf '%s\n' "$l" | grep -Eq '^[A-Za-z0-9<>+:,./-]+$' || return 1
  printf '%s' "$l"
}

# ---------------------------------------------------------------------------------------------
# .env handling (values live in variables named after the keys)

KEYS=""
load_env_file() {  # load_env_file FILE -> sets every KEY found
  local line key val
  [ -f "$1" ] || return 0
  while IFS= read -r line || [ -n "$line" ]; do
    case "$line" in ''|'#'*) continue ;; esac
    case "$line" in *=*) ;; *) continue ;; esac
    key="${line%%=*}"; val="${line#*=}"
    key="${key#export }"; key="$(printf '%s' "$key" | tr -d ' ')"
    printf '%s\n' "$key" | grep -Eq '^[A-Z_][A-Z0-9_]*$' || continue
    case "$val" in \"*\") val="${val#\"}"; val="${val%\"}" ;; \'*\') val="${val#\'}"; val="${val%\'}" ;; esac
    printf -v "$key" '%s' "$val"
    case " $KEYS " in *" $key "*) ;; *) KEYS="$KEYS $key" ;; esac
  done < "$1"
}
get() { eval "printf '%s' \"\${$1-}\""; }

write_env_file() {  # .env.example as the template: comments kept, values filled in
  local tmp line key extra=""
  tmp="$(mktemp "${ENV_FILE}.XXXXXX")"
  chmod 600 "$tmp"
  while IFS= read -r line || [ -n "$line" ]; do
    case "$line" in
      [A-Z]*=*) key="${line%%=*}"; printf '%s=%s\n' "$key" "$(get "$key")" >> "$tmp" ;;
      *) printf '%s\n' "$line" >> "$tmp" ;;
    esac
  done < "$EXAMPLE"
  for key in $KEYS; do   # keys the template does not know (kept, not lost)
    grep -q "^$key=" "$EXAMPLE" || extra="$extra $key"
  done
  if [ -n "$extra" ]; then
    printf '\n# Other keys found in the previous file\n' >> "$tmp"
    for key in $extra; do printf '%s=%s\n' "$key" "$(get "$key")" >> "$tmp"; done
  fi
  mv "$tmp" "$ENV_FILE"
}

set_env_key() {  # set_env_key KEY VALUE: update one key in the written file
  printf -v "$1" '%s' "$2"
  write_env_file
}

# ---------------------------------------------------------------------------------------------
# tools

PIO=""
find_pio() {
  if command -v pio >/dev/null 2>&1; then PIO="$(command -v pio)"
  elif [ -x "$HOME/.platformio/penv/bin/pio" ]; then PIO="$HOME/.platformio/penv/bin/pio"
  else PIO=""; fi
}

need_python() {
  command -v python3 >/dev/null 2>&1 || die "python3 is needed (macOS: xcode-select --install or python.org; Pi: sudo apt install python3)"
  if [ "$PLATFORM" = linux ] && ! python3 -c 'import ensurepip, venv' >/dev/null 2>&1; then
    info "python3-venv is missing."
    run sudo apt-get install -y python3-venv python3-pip
  fi
}

ensure_pio() {
  find_pio
  if [ -n "$PIO" ]; then ok "PlatformIO: $PIO"; return; fi
  info "PlatformIO is not installed. It installs into ~/.platformio (about 300 MB with the toolchains)."
  yesno "Install PlatformIO now?" y || die "PlatformIO is needed to build firmware."
  need_python
  local getter="$TOOLS/get-platformio.py"
  run mkdir -p "$TOOLS"
  run curl -fsSL -o "$getter" https://raw.githubusercontent.com/platformio/platformio-core-installer/master/get-platformio.py
  run python3 "$getter"
  PIO="$HOME/.platformio/penv/bin/pio"
}

ESPTOOL=""
TOOLS_PY=""
ensure_tools_venv() {  # esptool 5.x (read-flash / write-flash / --after watchdog-reset) and pyserial
  TOOLS_PY="$TOOLS/venv/bin/python"
  ESPTOOL="$TOOLS/venv/bin/esptool"
  if [ -x "$ESPTOOL" ]; then return; fi
  need_python
  info "Installing esptool and pyserial into $TOOLS/venv"
  run python3 -m venv "$TOOLS/venv"
  run "$TOOLS/venv/bin/pip" install --quiet --upgrade pip
  run "$TOOLS/venv/bin/pip" install --quiet "esptool>=5,<6" pyserial
}

pio_run() {  # pio_run <args...> with the chosen .env
  if [ "$DRY_RUN" = 1 ]; then printf '%s  + (cd %s && SPARK_ENV_FILE=%s %s %s)%s\n' "$D" "$ROOT" "$ENV_FILE" "${PIO:-pio}" "$*" "$N"; return 0; fi
  (cd "$ROOT" && SPARK_ENV_FILE="$ENV_FILE" "$PIO" "$@")
}

list_ports() {
  if [ "$PLATFORM" = mac ]; then ls /dev/cu.usbmodem* /dev/cu.usbserial* 2>/dev/null || true
  else ls /dev/ttyACM* /dev/ttyUSB* 2>/dev/null || true; fi
}

choose_port() {  # choose_port VAR "what"
  local ports first
  ports="$(list_ports)"
  first="$(printf '%s\n' "$ports" | head -n 1)"
  if [ -n "$ports" ]; then info "Serial ports found:"; printf '  %s\n' $ports; fi
  [ -z "$first" ] && first="$( [ "$PLATFORM" = mac ] && echo /dev/cu.usbmodem1101 || echo /dev/ttyACM0 )"
  ask "$1" "Serial port of the $2" "$first"
}

# ---------------------------------------------------------------------------------------------
# start

[ -f "$EXAMPLE" ] || die "$EXAMPLE is missing: run setup.sh from a SpArk checkout."
title "SpArk setup"
info "SpArkNode (roof node), SpArkKids (T-Deck Plus) and SpArk Room Bridge (Discord), based on MeshCore v1.18."
[ "$DRY_RUN" = 1 ] && warn "Dry run: answers go to $ENV_FILE; commands are printed, not run."
info "Configuration file: $ENV_FILE"

load_env_file "$EXAMPLE"
if [ -f "$ENV_FILE" ]; then info "Existing values from $ENV_FILE are offered as defaults."; load_env_file "$ENV_FILE"
elif [ "$DRY_RUN" = 1 ] && [ -f "$ROOT/.env" ]; then info "Existing values from $ROOT/.env are offered as defaults (it is not changed)."; load_env_file "$ROOT/.env"; fi

# 1. components ------------------------------------------------------------------------------
title "1. What do you want to set up?"
info "SpArkNode:    room server + relay firmware for the SenseCAP Solar Node P1 (the roof node)"
info "SpArk Room Bridge: the Discord service, on a Raspberry Pi or computer within Bluetooth range of the node"
info "SpArkKids:    messenger firmware for the LilyGO T-Deck Plus (kids, or anyone who wants it simple)"
DO_NODE=0; DO_BRIDGE=0; DO_KIDS=0
yesno "Set up SpArkNode?" y && DO_NODE=1
yesno "Set up SpArk Room Bridge?" y && DO_BRIDGE=1
yesno "Set up SpArkKids?" n && DO_KIDS=1
[ $((DO_NODE + DO_BRIDGE + DO_KIDS)) -gt 0 ] || { info "Nothing chosen. Bye."; exit 0; }

# 2. questions -------------------------------------------------------------------------------
title "2. Configuration"

if [ "$DO_NODE" = 1 ] || [ "$DO_KIDS" = 1 ]; then
  info ""
  info "${B}Radio and mesh${N} (must match your local MeshCore mesh)"
  info "  NL     Dutch MeshCore: 869.618 MHz, BW 62.5, SF7, CR5, scope 'nl', 2-byte path hashes"
  info "  EU     MeshCore EU/UK default: 869.525 MHz, BW 250, SF11, CR5, unscoped, 1-byte hashes"
  info "  custom enter every value"
  ask_match SPARK_RADIO_PRESET "Radio preset (NL, EU, custom)" "${SPARK_RADIO_PRESET:-NL}" '^(NL|EU|custom)$' "Type NL, EU or custom."
  case "$SPARK_RADIO_PRESET" in
    NL) SPARK_LORA_FREQ=869.618; SPARK_LORA_BW=62.5; SPARK_LORA_SF=7; SPARK_LORA_CR=5; SPARK_SCOPE=nl; SPARK_PATH_HASH_SIZE=2 ;;
    EU) SPARK_LORA_FREQ=869.525; SPARK_LORA_BW=250; SPARK_LORA_SF=11; SPARK_LORA_CR=5; SPARK_SCOPE=; SPARK_PATH_HASH_SIZE=1 ;;
    custom)
      ask_match SPARK_LORA_FREQ "Frequency (MHz)" "$SPARK_LORA_FREQ" '^[0-9]+(\.[0-9]+)?$' "A number, e.g. 869.618"
      ask_match SPARK_LORA_BW "Bandwidth (kHz)" "$SPARK_LORA_BW" '^[0-9]+(\.[0-9]+)?$' "A number, e.g. 62.5"
      ask_match SPARK_LORA_SF "Spreading factor (5-12)" "$SPARK_LORA_SF" '^([5-9]|1[0-2])$' "5 to 12"
      ask_match SPARK_LORA_CR "Coding rate (5-8)" "$SPARK_LORA_CR" '^[5-8]$' "5 to 8"
      ask_match SPARK_SCOPE "Flood scope / region name (empty = unscoped)" "$SPARK_SCOPE" '^[A-Za-z0-9_.-]*$' "Letters, digits, - _ . only"
      ask_match SPARK_PATH_HASH_SIZE "Path hash size in bytes (1-3)" "$SPARK_PATH_HASH_SIZE" '^[1-3]$' "1, 2 or 3"
      ;;
  esac
  ask_match SPARK_LORA_TX_POWER "Transmit power (dBm, max 22)" "$SPARK_LORA_TX_POWER" '^-?[0-9]+$' "A whole number"
fi

info ""
info "${B}Time zone${N} (SpArkKids clock, bridge health report)"
TZ_DEFAULT="$BRIDGE_TZ"
if [ -z "$TZ_DEFAULT" ] || { [ "$TZ_DEFAULT" = UTC ] && [ "${SPARK_TZ:-UTC0}" = UTC0 ]; }; then TZ_DEFAULT="$(system_tz)"; fi
while :; do
  ask BRIDGE_TZ "Time zone (Area/City, e.g. Europe/Amsterdam)" "$TZ_DEFAULT"
  if SPARK_TZ="$(posix_tz "$BRIDGE_TZ")"; then info "  firmware time zone (POSIX): $SPARK_TZ"; break; fi
  [ "$BRIDGE_TZ" = UTC ] && { SPARK_TZ=UTC0; break; }
  warn "Unknown zone $BRIDGE_TZ (no /usr/share/zoneinfo/$BRIDGE_TZ)."
  [ "$DEFAULTS" = 1 ] && { BRIDGE_TZ=UTC; SPARK_TZ=UTC0; warn "Using UTC."; break; }
done

if [ "$DO_NODE" = 1 ] || [ "$DO_KIDS" = 1 ] || [ "$DO_BRIDGE" = 1 ]; then
  info ""
  info "${B}SpArkNode${N}"
  ask_text SPARK_NODE_NAME "Node name (adverts, app, BLE 'MeshCore-<name>')" "${SPARK_NODE_NAME:-SparkNode}" 31
fi

if [ "$DO_NODE" = 1 ]; then
  ask_match SPARK_NODE_LAT "Advertised latitude (0.0 = none)" "$SPARK_NODE_LAT" '^-?[0-9]+(\.[0-9]+)?$' "A number"
  ask_match SPARK_NODE_LON "Advertised longitude (0.0 = none)" "$SPARK_NODE_LON" '^-?[0-9]+(\.[0-9]+)?$' "A number"
  while :; do
    ask_secret SPARK_ADMIN_PASSWORD "Admin password (max 15)" "$SPARK_ADMIN_PASSWORD" "$(rand_from 'a-km-np-z2-9' 12)"
    valid_text "$SPARK_ADMIN_PASSWORD" && [ -n "$SPARK_ADMIN_PASSWORD" ] && [ "${#SPARK_ADMIN_PASSWORD}" -le 15 ] && break
    warn "1 to 15 characters, no quotes, backslashes, backticks or \$."; SPARK_ADMIN_PASSWORD=""
  done
  while :; do
    ask_secret SPARK_ROOM_PASSWORD "Room password your tribe logs in with (max 15)" "$SPARK_ROOM_PASSWORD" "$(rand_from 'a-km-np-z2-9' 8)"
    valid_text "$SPARK_ROOM_PASSWORD" && [ -n "$SPARK_ROOM_PASSWORD" ] && [ "${#SPARK_ROOM_PASSWORD}" -le 15 ] && break
    warn "1 to 15 characters, no quotes, backslashes, backticks or \$."; SPARK_ROOM_PASSWORD=""
  done
  info "The BLE PIN protects the link to SpArk Room Bridge. Empty = BLE off until you set one on the node's USB console."
  while :; do
    ask_secret SPARK_BLE_PIN "BLE PIN (6 digits, not starting with 0)" "$SPARK_BLE_PIN" "$( [ "$DO_BRIDGE" = 1 ] && printf '%s%s' "$(rand_from 1-9 1)" "$(rand_from 0-9 5)" )"
    [ -z "$SPARK_BLE_PIN" ] && break
    printf '%s\n' "$SPARK_BLE_PIN" | grep -Eq '^[1-9][0-9]{5}$' && break
    warn "6 digits, the first not 0."; SPARK_BLE_PIN=""
  done
fi

if [ "$DO_KIDS" = 1 ]; then
  info ""
  info "${B}SpArkKids${N}"
  ask_text KIDS_ROOM_NAME "Room name on the kids' screen" "${KIDS_ROOM_NAME:-$SPARK_NODE_NAME}" 15
  if [ "$DO_NODE" = 0 ]; then
    while :; do
      ask_secret KIDS_ROOM_PASSWORD "The SpArkNode's room password" "${KIDS_ROOM_PASSWORD:-$SPARK_ROOM_PASSWORD}" ""
      [ -n "$KIDS_ROOM_PASSWORD" ] && valid_text "$KIDS_ROOM_PASSWORD" && break
      warn "Needed: the kids' devices log in with it."
      [ "$DEFAULTS" = 1 ] && { KIDS_ROOM_PASSWORD="$(rand_from 'a-km-np-z2-9' 8)"; warn "Dry-run placeholder used."; break; }
    done
  else
    KIDS_ROOM_PASSWORD=""   # follows SPARK_ROOM_PASSWORD
  fi
  while :; do
    ask_secret KIDS_PARENT_PIN "Parent PIN (4-8 digits; opens the parent settings)" "$KIDS_PARENT_PIN" "$(rand_from 0-9 4)"
    printf '%s\n' "$KIDS_PARENT_PIN" | grep -Eq '^[0-9]{4,8}$' && break
    warn "4 to 8 digits."; KIDS_PARENT_PIN=""
  done
  if [ -z "$KIDS_OTA_SIGNING_KEY" ]; then
    # Signs SpArkKids bundles for Bluetooth updates through the bridge (public half goes into the
    # firmware). Generated once and never shown; a new key needs one USB flash per T-Deck.
    KIDS_OTA_SIGNING_KEY="$(od -An -N32 -tx1 /dev/urandom | tr -d ' \n')"
    info "Generated KIDS_OTA_SIGNING_KEY (signs Bluetooth updates for the T-Decks; keep $ENV_FILE safe)."
  fi
  info "Public channel: MeshCore Public, readable by everyone nearby. Off by default; parents can"
  info "switch it on or off later on the device (Parent settings > Public channel)."
  if yesno "Public channel on by default?" "$( [ "${KIDS_PUBLIC_ENABLED:-0}" = 1 ] && echo y || echo n )"; then KIDS_PUBLIC_ENABLED=1; else KIDS_PUBLIC_ENABLED=0; fi
  ask_match KIDS_PUBLIC_PER_HOUR "Max Public posts per hour per device (1-16)" "${KIDS_PUBLIC_PER_HOUR:-6}" '^([1-9]|1[0-6])$' "1 to 16"
  if [ "$DO_NODE" = 1 ] && [ -z "$KIDS_ROOM_KEY" ]; then
    info "The room key (the node's public key) is read from the node after flashing it (step 4)."
  else
    ask_match KIDS_ROOM_KEY "SpArkNode public key (64 hex, node console: get public.key; empty = read it over USB later)" "$KIDS_ROOM_KEY" '^([0-9A-Fa-f]{64})?$' "64 hex digits, or empty"
  fi
fi

if [ "$DO_BRIDGE" = 1 ]; then
  info ""
  info "${B}SpArk Room Bridge${N}"
  info "Discord: create a dedicated application + bot (see spark/README.md 'Discord setup')."
  ask_secret DISCORD_TOKEN "Discord bot token" "$DISCORD_TOKEN" ""
  [ -z "$DISCORD_TOKEN" ] && warn "No token: the bridge will not start until DISCORD_TOKEN is set in $ENV_FILE."
  ask_match BRIDGE_GUILD_IDS "Discord server id(s), comma separated (empty = acts nowhere)" "$BRIDGE_GUILD_IDS" '^[0-9, ]*$' "Digits and commas only"
  ask_match BRIDGE_TRANSPORT "Link to the node: ble or serial" "${BRIDGE_TRANSPORT:-ble}" '^(ble|serial|tcp)$' "ble or serial"
  if [ "$BRIDGE_TRANSPORT" = ble ]; then
    ask_text BRIDGE_BLE_NAME "BLE name to look for (substring of MeshCore-<name>)" "${BRIDGE_BLE_NAME:-$SPARK_NODE_NAME}" 31
    if [ "$PLATFORM" = linux ]; then ask BRIDGE_BLE_ADDRESS "Node BLE MAC address (optional)" "$BRIDGE_BLE_ADDRESS"; fi
  elif [ "$BRIDGE_TRANSPORT" = serial ]; then
    choose_port BRIDGE_SERIAL_PORT "node (for the bridge)"
  fi
  ask BRIDGE_DEFAULT_PER_MINUTE "Default messages per minute for a new Discord channel" "${BRIDGE_DEFAULT_PER_MINUTE:-3}"
  info ""
  info "MQTT observer (optional): publish every packet the node hears to the DutchMeshCore"
  info "collectors (MC-Radar) or Let's Mesh. Needs SpArkNode1.3. The bridge signs its login with"
  info "the node's own key over the node link; there is no account or password for these brokers."
  case "$BRIDGE_OBSERVER" in 1|true|yes|on) __obs=y ;; *) __obs=n ;; esac
  if yesno "Enable the MQTT observer?" "$__obs"; then
    BRIDGE_OBSERVER=1
    ask_match BRIDGE_OBSERVER_BROKERS "Broker(s): dutchmeshcore, letsmesh-eu, letsmesh-us or mqtts://host:port, comma separated" \
      "${BRIDGE_OBSERVER_BROKERS:-dutchmeshcore}" '^[A-Za-z0-9:/._, -]+$' "Preset names or URLs, comma separated"
    ask_match BRIDGE_OBSERVER_IATA "Region code (IATA airport code, e.g. AMS, RTM, EIN)" "${BRIDGE_OBSERVER_IATA:-AMS}" \
      '^[A-Za-z]{3,4}$' "3 or 4 letters"
    BRIDGE_OBSERVER_IATA="$(printf '%s' "$BRIDGE_OBSERVER_IATA" | tr '[:lower:]' '[:upper:]')"
    ask_text BRIDGE_OBSERVER_NAME "Observer name shown on the map (empty = the node name)" "$BRIDGE_OBSERVER_NAME" 31
    ask_match BRIDGE_OBSERVER_OWNER "Your companion's public key, links the observer to you (64 hex, optional)" \
      "$BRIDGE_OBSERVER_OWNER" '^([0-9A-Fa-f]{64})?$' "64 hex digits, or empty"
    case "$BRIDGE_OBSERVER_BROKERS" in *://*)
      info "A URL broker with a user name uses password login; the password goes in a variable you name."
      ask BRIDGE_OBSERVER_USERNAME "MQTT user name (empty = node-signed token)" "$BRIDGE_OBSERVER_USERNAME"
      if [ -n "$BRIDGE_OBSERVER_USERNAME" ]; then
        ask_match BRIDGE_OBSERVER_PASSWORD_ENV "Name of the variable that holds the password" \
          "${BRIDGE_OBSERVER_PASSWORD_ENV:-BRIDGE_OBSERVER_PASSWORD}" '^[A-Z_][A-Z0-9_]*$' "An environment variable name"
        warn "Put ${BRIDGE_OBSERVER_PASSWORD_ENV}=<password> in $ENV_FILE or the service environment yourself."
      fi ;;
    esac
  else
    BRIDGE_OBSERVER=0
  fi
fi

# write .env ----------------------------------------------------------------------------------
for k in SPARK_RADIO_PRESET SPARK_LORA_FREQ SPARK_LORA_BW SPARK_LORA_SF SPARK_LORA_CR SPARK_LORA_TX_POWER SPARK_SCOPE \
         SPARK_PATH_HASH_SIZE SPARK_NODE_NAME SPARK_NODE_LAT SPARK_NODE_LON SPARK_ADMIN_PASSWORD SPARK_ROOM_PASSWORD \
         SPARK_BLE_PIN KIDS_ROOM_KEY KIDS_ROOM_NAME KIDS_ROOM_PASSWORD KIDS_PARENT_PIN KIDS_OTA_SIGNING_KEY KIDS_PUBLIC_ENABLED \
         KIDS_PUBLIC_PER_HOUR DISCORD_TOKEN BRIDGE_GUILD_IDS BRIDGE_TRANSPORT BRIDGE_BLE_NAME BRIDGE_BLE_ADDRESS \
         BRIDGE_SERIAL_PORT BRIDGE_DEFAULT_PER_MINUTE BRIDGE_TZ SPARK_TZ BRIDGE_OBSERVER BRIDGE_OBSERVER_BROKERS \
         BRIDGE_OBSERVER_IATA BRIDGE_OBSERVER_NAME BRIDGE_OBSERVER_OWNER BRIDGE_OBSERVER_USERNAME \
         BRIDGE_OBSERVER_PASSWORD_ENV; do
  case " $KEYS " in *" $k "*) ;; *) KEYS="$KEYS $k" ;; esac
done
write_env_file
ok "Wrote $ENV_FILE (mode 600, gitignored)."

# 3+4. firmware -------------------------------------------------------------------------------
NODE_UF2="$ROOT/.pio/build/$NODE_ENV/firmware.uf2"
KIDS_FACTORY="$ROOT/.pio/build/$KIDS_ENV/firmware-merged.bin"
KIDS_APP="$ROOT/.pio/build/$KIDS_ENV/firmware.bin"
KIDS_OTA="$ROOT/.pio/build/$KIDS_ENV/kids-ota.zip"
# otadata that boots app0: written with an app-only flash, so a T-Deck that last took a Bluetooth
# update (running from app1) starts the newly flashed image
KIDS_BOOT_APP0="$HOME/.platformio/packages/framework-arduinoespressif32/tools/partitions/boot_app0.bin"
NODE_ZIP="$ROOT/.pio/build/$NODE_ENV/firmware-dfu.zip"

find_uf2_drive() {
  local d
  for d in /Volumes/XIAO-BOOT* /Volumes/*BOOT* /media/"$USER"/* /run/media/"$USER"/* /media/*; do
    [ -f "$d/INFO_UF2.TXT" ] && { printf '%s' "$d"; return 0; }
  done
  return 1
}

flash_node() {
  title "4. Flash SpArkNode"
  info "1. Connect the SenseCAP Solar Node P1 to this computer over USB."
  info "2. Double-press its reset button quickly. A drive called XIAO-BOOT (or similar) appears."
  info "   (Existing node? Its key, settings, ACL and stored posts are kept. Settings from .env"
  info "    apply only to a new node; change an existing one on its console.)"
  if [ "$DRY_RUN" = 1 ]; then
    printf '%s  + wait for the UF2 drive (INFO_UF2.TXT), then: cp %s <drive>/ && sync%s\n' "$D" "$NODE_UF2" "$N"
    return
  fi
  yesno "Flash the node now?" y || { info "Later: copy $NODE_UF2 to the UF2 drive."; return; }
  local drive="" i
  info "Waiting for the UF2 drive (2 minutes)..."
  for i in $(seq 1 120); do drive="$(find_uf2_drive || true)"; [ -n "$drive" ] && break; sleep 1; done
  if [ -z "$drive" ]; then
    warn "No UF2 drive found (Raspberry Pi OS Lite does not mount it automatically)."
    ask drive "Path where the drive is mounted (empty = skip)" ""
    [ -n "$drive" ] || { info "Skipped. Copy $NODE_UF2 to the drive yourself."; return; }
  fi
  info "Copying to $drive ..."
  cp "$NODE_UF2" "$drive/" || true   # the drive disappears as soon as the copy completes
  sync || true
  ok "SpArkNode firmware copied. The node reboots by itself."
}

# Update over Bluetooth: spark-bridge update (host/src/spark_bridge/update.py) pauses a running
# bridge, checks package and node, sends dfu, transfers to the bootloader and verifies the build.
update_node_ble() {
  title "4. Update SpArkNode over Bluetooth"
  local venv="$ROOT/host/.venv"
  if [ ! -x "$venv/bin/spark-bridge" ]; then
    info "spark-bridge is not installed yet; installing it into $venv."
    need_python
    [ -x "$venv/bin/python" ] || run python3 -m venv "$venv"
    run "$venv/bin/pip" install --quiet -e "$ROOT/host"
  fi
  info "The node must run SpArkNode1.1 or later (SpArkNode1 has no dfu command: use usb once)."
  info "The bridge host must be paired with the node (BRIDGE_TRANSPORT=ble) or connected by USB."
  info "Battery at least 3.7 V; the transfer takes about 3 minutes; keep this computer awake."
  run "$venv/bin/spark-bridge" update --env "$ENV_FILE" "$NODE_ZIP" || \
    warn "Update not completed. The message above says whether the old app is intact. See spark/README.md 'Update over Bluetooth'."
}

# One-time: replace the node's UF2 bootloader with the OTA-safe SpArk build (spark/bootloader).
install_bootloader() {
  local bl="$ROOT/out/spark/bootloader/update-xiao_nrf52840_ble_bootloader-0.11.0-spark1_nosd.uf2" drive="" i
  info ""
  info "${B}OTA-safe bootloader (optional, one time, over USB)${N}"
  info "  The stock bootloader falls back to USB-only mode when a Bluetooth update breaks off: on a"
  info "  roof that means a ladder. The SpArk build (Adafruit bootloader 0.11.0 + DEFAULT_TO_OTA_DFU)"
  info "  then keeps waiting for a Bluetooth retry instead. Double reset still opens XIAO-BOOT."
  info "  Risks: an interrupted bootloader copy can brick the node (recovery: an SWD programmer);"
  info "  room posts stored on the node may be lost (the copy uses part of the post store)."
  info "  Already installed? Skip it (INFO_UF2.TXT on XIAO-BOOT shows UF2 Bootloader 0.11.0-spark1)."
  yesno "Install the OTA-safe bootloader now?" n || return 0
  run "$ROOT/spark/bootloader/build.sh" || die "Bootloader build failed; nothing was flashed."
  if [ "$DRY_RUN" = 1 ]; then
    printf '%s  + wait for the UF2 drive, check Board-ID nRF52840-SeeedXiao-v1 in INFO_UF2.TXT, cp %s <drive>/%s\n' "$D" "$bl" "$N"
    return
  fi
  info "Connect the node over USB with the batteries in, and double-press its reset button."
  for i in $(seq 1 120); do drive="$(find_uf2_drive || true)"; [ -n "$drive" ] && break; sleep 1; done
  [ -n "$drive" ] || { warn "No UF2 drive found; bootloader not installed."; return 0; }
  cat "$drive/INFO_UF2.TXT"
  if ! grep -q "Board-ID: nRF52840-SeeedXiao-v1" "$drive/INFO_UF2.TXT"; then
    warn "This bootloader reports a different Board-ID: the SpArk build is not for this board. Not installed."
    return 0
  fi
  if grep -q "0.11.0-spark1" "$drive/INFO_UF2.TXT"; then ok "The OTA-safe bootloader is already installed."; return 0; fi
  local typed=""
  read -r -p "Type 'install' to write the bootloader (do not unplug or reset for 30 s): " typed || true
  [ "$typed" = install ] || { info "Not installed."; return 0; }
  cp "$bl" "$drive/" || true   # the drive goes away when the copy completes
  sync || true
  info "Waiting 15 s while the new bootloader is copied into place..."; sleep 15
  ok "Done. To check: double-press reset; INFO_UF2.TXT must show 0.11.0-spark1. Then press reset once."
  info "If room posts look damaged afterwards: node console 'posts reset' (USB)."
}

read_node_key() {
  local port key
  info "The kids' devices need the SpArkNode's public key."
  yesno "Read it from the node over USB now?" y || return 0
  ensure_tools_venv
  choose_port port "SpArkNode"
  if [ "$DRY_RUN" = 1 ]; then printf '%s  + %s spark/tools/node_cli.py --port %s --public-key%s\n' "$D" "$TOOLS_PY" "$port" "$N"; return; fi
  info "Waiting 10 s for the node to boot..."; sleep 10
  if key="$("$TOOLS_PY" "$ROOT/spark/tools/node_cli.py" --port "$port" --public-key)"; then
    set_env_key KIDS_ROOM_KEY "$key"
    ok "KIDS_ROOM_KEY saved: ${key:0:8}..."
  else
    warn "Could not read the key. Put it in $ENV_FILE as KIDS_ROOM_KEY (node console: get public.key)."
  fi
}

if [ "$DO_NODE" = 1 ] || [ "$DO_KIDS" = 1 ]; then ensure_pio; fi

if [ "$DO_NODE" = 1 ]; then
  title "3. Build SpArkNode"
  pio_run run -e "$NODE_ENV"
  pio_run run -e "$NODE_ENV" -t create_uf2 -t dfu_zip
  [ "$DRY_RUN" = 1 ] || ok "Built $NODE_UF2 and $NODE_ZIP"
  install_bootloader
  info ""
  info "Install SpArkNode:"
  info "  usb  copy the UF2 to the node's XIAO-BOOT drive (needs a USB cable; always works)"
  info "  ble  update over Bluetooth with spark-bridge update (the node must already run SpArkNode1.1"
  info "       or later; the bridge is paused while it runs)"
  info "  skip do it later"
  ask_match NODE_INSTALL "How" "usb" '^(usb|ble|skip)$' "Type usb, ble or skip."
  case "$NODE_INSTALL" in
    usb) flash_node ;;
    ble) update_node_ble ;;
    *) info "Later: copy $NODE_UF2 to the UF2 drive, or: host/.venv/bin/spark-bridge update $NODE_ZIP" ;;
  esac
fi

flash_kids() {
  title "4. Flash SpArkKids"
  ensure_tools_venv
  info "Put the T-Deck Plus in download mode:"
  info "  switch it off, connect USB, hold the trackball pressed, switch it on, release the trackball."
  pause "T-Deck in download mode?"
  local port mac dir first=n h1 h2
  choose_port port "T-Deck Plus"
  if yesno "Is this the first SpArk flash on this T-Deck?" y; then first=y; fi
  if [ "$first" = y ]; then
    info "Before the first flash, back up the whole 16 MB flash (the original firmware, its settings"
    info "and any licence). Never erase the device; this backup restores it bit for bit."
    if yesno "Make the backup (read twice, compare hashes)?" y; then
      if [ "$DRY_RUN" = 1 ]; then mac="<mac>"
      else mac="$("$ESPTOOL" --chip esp32s3 -p "$port" --after no-reset read-mac 2>/dev/null | awk '/MAC:/{print $2; exit}' | tr -d ':')"; fi
      dir="$ROOT/backups/tdeck-${mac:-unknown}-$(date +%Y%m%d-%H%M%S)"
      run mkdir -p "$dir"
      run "$ESPTOOL" --chip esp32s3 -p "$port" -b 921600 --after no-reset read-flash 0x0 0x1000000 "$dir/full-1.bin"
      run "$ESPTOOL" --chip esp32s3 -p "$port" -b 921600 --after no-reset read-flash 0x0 0x1000000 "$dir/full-2.bin"
      if [ "$DRY_RUN" = 1 ]; then printf '%s  + compare sha256 of full-1.bin and full-2.bin%s\n' "$D" "$N"
      else
        h1="$(shasum -a 256 "$dir/full-1.bin" 2>/dev/null | cut -d' ' -f1 || sha256sum "$dir/full-1.bin" | cut -d' ' -f1)"
        h2="$(shasum -a 256 "$dir/full-2.bin" 2>/dev/null | cut -d' ' -f1 || sha256sum "$dir/full-2.bin" | cut -d' ' -f1)"
        if [ -n "$h1" ] && [ "$h1" = "$h2" ]; then
          printf '%s  full-1.bin\n%s  full-2.bin\n' "$h1" "$h2" > "$dir/SHA256SUMS"
          ok "Backup verified: $dir (sha256 $h1)"
          info "Restore later with: $ESPTOOL --chip esp32s3 -p <port> -b 921600 write-flash 0x0 $dir/full-1.bin"
        else
          die "The two backups differ ($h1 / $h2). Not flashing. Check the cable and run setup.sh again."
        fi
      fi
    fi
    run "$ESPTOOL" --chip esp32s3 -p "$port" -b 921600 --after watchdog-reset write-flash 0x0 "$KIDS_FACTORY"
  else
    run "$ESPTOOL" --chip esp32s3 -p "$port" -b 921600 --after watchdog-reset write-flash 0xe000 "$KIDS_BOOT_APP0" 0x10000 "$KIDS_APP"
  fi
  [ "$DRY_RUN" = 1 ] || ok "SpArkKids flashed. The T-Deck restarts and asks for a name."
  info "Parent settings: People > gear > Parent settings, PIN from KIDS_PARENT_PIN."
}

if [ "$DO_KIDS" = 1 ]; then
  if [ -z "$(get KIDS_ROOM_KEY)" ]; then read_node_key; fi
  if [ -z "$(get KIDS_ROOM_KEY)" ] && [ "$DRY_RUN" = 0 ]; then
    warn "KIDS_ROOM_KEY is still empty: SpArkKids cannot be built. Add it to $ENV_FILE and run setup.sh again."
  else
    title "3. Build SpArkKids"
    if [ "$DRY_RUN" = 1 ] && [ -z "$(get KIDS_ROOM_KEY)" ]; then info "(the real run builds once KIDS_ROOM_KEY is known)"; fi
    pio_run run -e "$KIDS_ENV"
    pio_run run -e "$KIDS_ENV" -t mergebin -t kids_ota
    [ "$DRY_RUN" = 1 ] || ok "Built $KIDS_FACTORY, $KIDS_APP and $KIDS_OTA"
    info "Bluetooth updates: the bridge offers $KIDS_OTA to T-Decks near it once installed there:"
    info "  host/.venv/bin/spark-bridge kids-firmware install $KIDS_OTA"
    if yesno "Flash a T-Deck Plus now?" y; then flash_kids
    else info "Later: ./setup.sh again, or esptool write-flash 0x0 $KIDS_FACTORY (first time) / 0x10000 $KIDS_APP."; fi
  fi
fi

# 5. bridge -----------------------------------------------------------------------------------
install_bridge() {
  title "5. Install SpArk Room Bridge"
  need_python
  if [ "$PLATFORM" = linux ] && ! command -v bluetoothctl >/dev/null 2>&1; then run sudo apt-get install -y bluez; fi
  local venv="$ROOT/host/.venv"
  [ -x "$venv/bin/python" ] || run python3 -m venv "$venv"
  run "$venv/bin/pip" install --quiet --upgrade pip
  run "$venv/bin/pip" install --quiet -e "$ROOT/host"
  ok "SpArk Room Bridge installed in $venv"

  if [ "$(get BRIDGE_TRANSPORT)" = ble ]; then
    info ""
    info "Pair the node once at OS level, with the BLE PIN (SPARK_BLE_PIN or the one set on the node):"
    if [ "$PLATFORM" = mac ]; then
      info "  macOS shows a pairing dialog on the first connect. Start the bridge once in Terminal so macOS"
      info "  also asks for Bluetooth permission:  $venv/bin/spark-bridge --env $ENV_FILE --no-discord"
    else
      info "  bluetoothctl  then: scan on / pair <MAC> / (PIN) / trust <MAC> / quit"
    fi
  fi

  if [ "$PLATFORM" = linux ]; then
    yesno "Install the systemd service spark-bridge (runs as $USER)?" y || return 0
    local unit="$TOOLS/spark-bridge.service"
    run mkdir -p "$TOOLS"
    run_sh "sed -e 's|@USER@|$USER|g' -e 's|@ROOT@|$ROOT|g' -e 's|@ENV_FILE@|$ENV_FILE|g' '$ROOT/host/deploy/spark-bridge.service' > '$unit'"
    run sudo usermod -aG bluetooth "$USER"
    run sudo install -m 644 "$unit" /etc/systemd/system/spark-bridge.service
    run sudo systemctl daemon-reload
    if [ -n "$(get DISCORD_TOKEN)" ] && yesno "Enable and start it now?" y; then
      run sudo systemctl enable --now spark-bridge
      info "Logs: journalctl -u spark-bridge -f"
    else
      info "Start later: sudo systemctl enable --now spark-bridge"
    fi
  else
    local label="com.kaperkunde.spark-bridge" plist="$HOME/Library/LaunchAgents/com.kaperkunde.spark-bridge.plist"
    local log="$HOME/Library/Logs/spark-bridge.log"
    yesno "Install the launchd agent (starts at login, restarts on exit)?" y || return 0
    if [ -f "$plist" ]; then
      warn "$plist exists (an earlier bridge?). Two bridges must not run with the same bot token."
      yesno "Replace it?" n || return 0
      run launchctl unload "$plist"
    fi
    run mkdir -p "$HOME/Library/LaunchAgents"
    run_sh "sed -e 's|@ROOT@|$ROOT|g' -e 's|@ENV_FILE@|$ENV_FILE|g' -e 's|@LOG@|$log|g' '$ROOT/host/deploy/$label.plist' > '$plist'"
    if [ -n "$(get DISCORD_TOKEN)" ] && yesno "Load it now?" y; then
      run launchctl load "$plist"
      info "Logs: tail -f $log"
    else
      info "Load later: launchctl load $plist"
    fi
  fi
}

[ "$DO_BRIDGE" = 1 ] && install_bridge

title "Done"
info "Configuration: $ENV_FILE"
[ "$DO_NODE" = 1 ] && info "SpArkNode: on the node's USB console 'ver' shows v1.18.0-SpArkNode1.3. Later updates: ./setup.sh (ble)."
[ "$DO_BRIDGE" = 1 ] && info "SpArk Room Bridge: in Discord, run /spark status in a channel, then /spark receive both."
[ "$DO_KIDS" = 1 ] && info "SpArkKids: type a name on the T-Deck; it logs in to the room by itself."
info "More: spark/README.md"
exit 0
