# Spark: PlatformIO pre-script. Reads the repository's .env and turns it into build flags for the
# SparkNode and SparkKids firmware. Used by variants/spark_p1 and variants/spark_tdeck through
#   extra_scripts = pre:spark/pio_env.py
#   custom_spark_component = node | kids
#
# The .env file is gitignored; .env.example documents every key. SPARK_ENV_FILE=<path> picks a
# different file (setup.sh --dry-run and CI use that). Values never appear in committed files.
import datetime
import hashlib
import os
import re
import subprocess
import sys

Import("env")  # noqa: F821  (SCons global)

ROOT = env.subst("$PROJECT_DIR")  # noqa: F821


def fail(msg):
    sys.stderr.write("\n*** Spark .env: %s\n\n" % msg)
    env.Exit(1)  # noqa: F821


def read_env(path):
    vals = {}
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            key, val = key.strip(), val.strip()
            if key.startswith("export "):
                key = key[7:].strip()
            if len(val) >= 2 and val[0] == val[-1] and val[0] in "'\"":
                val = val[1:-1]
            vals[key] = val
    return vals


component = env.GetProjectOption("custom_spark_component", "")  # noqa: F821
if component not in ("node", "kids"):
    fail("custom_spark_component must be 'node' or 'kids' (got %r)" % component)

env_file = os.environ.get("SPARK_ENV_FILE") or os.path.join(ROOT, ".env")
if not os.path.isfile(env_file):
    fail("%s not found. Run ./setup.sh, or: cp .env.example .env and fill it in." % env_file)
cfg = read_env(env_file)


def get(key, default=None, required=False):
    v = cfg.get(key, "")
    if v == "":
        if required:
            fail("%s is empty in %s" % (key, env_file))
        return default
    return v


def num(key, default, kind=float, lo=None, hi=None):
    v = get(key, default)
    try:
        n = kind(v)
    except ValueError:
        fail("%s must be a number, got %r" % (key, v))
    if (lo is not None and n < lo) or (hi is not None and n > hi):
        fail("%s=%s is out of range %s..%s" % (key, v, lo, hi))
    return v


def text(key, default=None, required=False, maxlen=31):
    v = get(key, default, required)
    if v is None:
        return None
    if len(v) > maxlen:
        fail("%s is longer than %d characters" % (key, maxlen))
    if re.search(r"['\"\\`$]", v):
        fail("%s must not contain quotes, backslashes, backticks or $" % key)
    return v


flags = {}      # macro -> value as it appears after -D NAME=
strings = {}    # macro -> plain string (quoted below)

# radio (SparkNode and SparkKids must match)
flags["LORA_FREQ"] = num("SPARK_LORA_FREQ", "869.618", float, 137, 1020)
flags["LORA_BW"] = num("SPARK_LORA_BW", "62.5", float, 7, 500)
flags["LORA_SF"] = num("SPARK_LORA_SF", "7", int, 5, 12)
flags["LORA_CR"] = num("SPARK_LORA_CR", "5", int, 5, 8)
flags["LORA_TX_POWER"] = num("SPARK_LORA_TX_POWER", "22", int, -9, 30)
scope = text("SPARK_SCOPE", "", maxlen=30)
if scope and not re.match(r"^[A-Za-z0-9_.-]+$", scope):
    fail("SPARK_SCOPE may only contain letters, digits, '-', '_' and '.'")
path_hash = int(num("SPARK_PATH_HASH_SIZE", "2", int, 1, 3))

if component == "node":
    strings["ADVERT_NAME"] = text("SPARK_NODE_NAME", "SparkNode", maxlen=31)
    flags["ADVERT_LAT"] = num("SPARK_NODE_LAT", "0.0", float, -90, 90)
    flags["ADVERT_LON"] = num("SPARK_NODE_LON", "0.0", float, -180, 180)
    strings["ADMIN_PASSWORD"] = text("SPARK_ADMIN_PASSWORD", required=True, maxlen=15)
    guest = text("SPARK_ROOM_PASSWORD", "", maxlen=15)
    if guest:
        strings["ROOM_PASSWORD"] = guest
    if scope:
        strings["DEFAULT_FLOOD_SCOPE_NAME"] = scope
    flags["SPARK_PATH_HASH_MODE"] = str(path_hash - 1)
    pin = get("SPARK_BLE_PIN", "")
    if pin:
        if not re.match(r"^[1-9][0-9]{5}$", pin):
            fail("SPARK_BLE_PIN must be 6 digits, not starting with 0 (or empty: BLE stays off)")
        flags["SPARK_BLE_PIN"] = pin

if component == "kids":
    key = get("KIDS_ROOM_KEY", required=True).replace(" ", "")
    if not re.match(r"^[0-9A-Fa-f]{64}$", key):
        fail("KIDS_ROOM_KEY must be the SparkNode's public key: 64 hex digits "
             "(node USB console: get public.key)")
    strings["KIDS_ROOM_KEY"] = key.upper()
    strings["KIDS_ROOM_NAME"] = text("KIDS_ROOM_NAME", get("SPARK_NODE_NAME", "SparkNode"), maxlen=15)
    room_pw = text("KIDS_ROOM_PASSWORD", get("SPARK_ROOM_PASSWORD", ""), maxlen=15)
    if not room_pw:
        fail("KIDS_ROOM_PASSWORD (or SPARK_ROOM_PASSWORD) must be set: the kids' devices log in with it")
    strings["KIDS_ROOM_PASSWORD"] = room_pw
    parent_pin = get("KIDS_PARENT_PIN", required=True)
    if not re.match(r"^[0-9]{4,8}$", parent_pin):
        fail("KIDS_PARENT_PIN must be 4 to 8 digits")
    strings["KIDS_ADMIN_PIN"] = parent_pin
    strings["KIDS_SCOPE_NAME"] = scope
    flags["KIDS_PATH_HASH_SIZE"] = str(path_hash)
    pub = get("KIDS_PUBLIC_ENABLED", "0")
    if pub not in ("0", "1"):
        fail("KIDS_PUBLIC_ENABLED must be 0 or 1")
    flags["KIDS_PUBLIC_ENABLED"] = pub
    flags["KIDS_PUBLIC_PER_HOUR"] = num("KIDS_PUBLIC_PER_HOUR", "6", int, 1, 16)
    tz = text("SPARK_TZ", "UTC0", maxlen=60)
    if not re.match(r"^[A-Za-z0-9<>+\-:,./]+$", tz):
        fail("SPARK_TZ must be a POSIX TZ string, e.g. CET-1CEST,M3.5.0,M10.5.0/3 or UTC0")
    strings["KIDS_TZ"] = tz

for k, v in strings.items():
    flags[k] = "'\"%s\"'" % v

# Drop any definition of these macros inherited from the base envs, then add ours.
names = set(flags)


def strip(flag):
    for n in names:
        flag = re.sub(r"(^|\s)-D\s*%s(=\S+)?(?=\s|$)" % re.escape(n), " ", flag)
    return flag.strip()


env.Replace(BUILD_FLAGS=[f for f in (strip(x) for x in env.get("BUILD_FLAGS", [])) if f])  # noqa: F821
env.Append(BUILD_FLAGS=["-D %s=%s" % (k, v) for k, v in sorted(flags.items())])  # noqa: F821
print("Spark: %s firmware configured from %s" % (component, os.path.relpath(env_file, ROOT)))


# ---- SparkNode build id -------------------------------------------------------------------------
# "<git hash>[+]-<UTC yymmddHHMMSS>" in a generated header, so only HostLink.cpp recompiles. The id
# is kept while the source (git commit + uncommitted changes) and the configuration stay the
# same, so the separate `pio run` invocations of one build (firmware, UF2, DFU zip) agree, and
# it changes with any of them. The node reports it (`get build`, `ver`); the updater reads it
# from the image ("SPARK-BUILD <version> <id>") and checks it after an update.

def git(*args):
    try:
        return subprocess.run(["git", "-C", ROOT] + list(args), capture_output=True, check=True).stdout
    except Exception:  # noqa: BLE001
        return b""


def build_id():
    head = git("rev-parse", "--short=8", "HEAD").decode().strip() or "nogit"
    diff = git("diff", "HEAD", "--", ".", ":(exclude)host")
    untracked = git("ls-files", "--others", "--exclude-standard", "--", "src", "examples", "variants", "lib",
                    "spark", "boards")
    fp = hashlib.sha256()
    for part in (head.encode(), diff, untracked, repr(sorted(flags.items())).encode(),
                 repr(env.get("BUILD_FLAGS", [])).encode()):  # noqa: F821
        fp.update(part)
        fp.update(b"\0")
    fingerprint = fp.hexdigest()
    gen = os.path.join(env.subst("$BUILD_DIR"), "spark_gen")  # noqa: F821
    os.makedirs(gen, exist_ok=True)
    cache = os.path.join(gen, "build_id.txt")
    try:
        with open(cache, encoding="utf-8") as f:
            old_fp, old_id = f.read().split()
        if old_fp == fingerprint:
            return gen, old_id
    except (OSError, ValueError):
        pass
    bid = "%s%s-%s" % (head, "+" if diff.strip() or untracked.strip() else "",
                       datetime.datetime.now(datetime.timezone.utc).strftime("%y%m%d%H%M%S"))
    with open(cache, "w", encoding="utf-8") as f:
        f.write("%s %s\n" % (fingerprint, bid))
    return gen, bid


if component == "node":
    gen_dir, bid = build_id()
    header = os.path.join(gen_dir, "spark_build_id.h")
    text = "#pragma once\n#define SPARK_BUILD_ID \"%s\"\n" % bid
    try:
        with open(header, encoding="utf-8") as f:
            same = f.read() == text
    except OSError:
        same = False
    if not same:
        with open(header, "w", encoding="utf-8") as f:
            f.write(text)
    env.Append(CPPPATH=[gen_dir])  # noqa: F821
    print("Spark: build id %s" % bid)

    # `pio run -e <env> -t dfu_zip` -> firmware-dfu.zip next to firmware.hex: the application
    # package for BLE OTA (spark-bridge update, or a phone with nRF Connect / nRF DFU).
    def dfu_zip_action(source, target, env):  # noqa: ARG001
        sd = env.BoardConfig().get("build.softdevice.sd_fwid", "0x0123")
        nrfutil = os.path.join(env.PioPlatform().get_package_dir("tool-adafruit-nrfutil") or "",
                               "adafruit-nrfutil.py")
        env.Execute(" ".join(['"$PYTHONEXE"', '"%s"' % nrfutil, "dfu", "genpkg", "--dev-type", "0x0052",
                              "--sd-req", sd, "--application", '"$BUILD_DIR/firmware.hex"',
                              '"$BUILD_DIR/firmware-dfu.zip"']))

    env.AddCustomTarget(  # noqa: F821
        name="dfu_zip",
        dependencies="$BUILD_DIR/firmware.hex",
        actions=dfu_zip_action,
        title="Create DFU zip",
        description="adafruit-nrfutil genpkg: application package for BLE OTA",
        always_build=True,
    )
