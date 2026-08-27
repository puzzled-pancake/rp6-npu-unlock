#!/usr/bin/env bash
# flash_unlock.sh - flash the RP6 CDSP/NPU unlock dtbo via Qualcomm EDL.
#
# Prereqs: python3, bkerler/edl (https://github.com/bkerler/edl), and a
# firehose programmer for QCM8550 (NOT included here - obtain your own).
#
# Usage:
#   EDL="python3 edl.py" LOADER=path/to/firehose.elf ./flash_unlock.sh dtbo_unlock_marker.bin
#   ... or ... ./flash_unlock.sh dtbo_unlock_enable.bin
set -euo pipefail

IMG=${1:?usage: flash_unlock.sh <image.bin>}
EDL=${EDL:-python3 edl.py}     # may contain spaces only if quoted as a single string
LOADER=${LOADER:?set LOADER=/path/to/firehose.elf}
FORCE=${FORCE:-0}

trap 'echo "flash_unlock: step failed - device NOT reset; re-enter EDL and retry, or reflash your stock dump" >&2' ERR

# ---- pre-flight: refuse obviously wrong files BEFORE touching the device ----
[ -f "$IMG" ] || { echo "no such file: $IMG"; exit 1; }
MAGIC=$(head -c 4 "$IMG" | od -An -tx1 | tr -d ' \n')
if [ "$MAGIC" != "d7b7ab1e" ]; then
  echo "refusing: $IMG does not start with the QTI dtbo magic (d7b7ab1e); got $MAGIC"
  [ "$FORCE" = "1" ] || exit 1
  echo "FORCE=1 - continuing anyway"
fi
SIZE=$(stat -c %s "$IMG" 2>/dev/null || stat -f %z "$IMG")
if [ "$SIZE" != "25165824" ]; then
  echo "warning: $IMG is $SIZE bytes; the RP6 dtbo partition is 25165824 (24 MiB)."
  echo "         A partial image flashes partially and the readback check will fail."
  [ "$FORCE" = "1" ] || exit 1
  echo "FORCE=1 - continuing anyway"
fi

wait_edl() {
  for _ in $(seq 1 60); do
    sleep 2
    if $EDL nop --loader="$LOADER" >/dev/null 2>&1; then return 0; fi
  done
  return 1
}

echo "== waiting for EDL (power the device off, then hold the EDL entry method) =="
if ! wait_edl; then echo "EDL not found"; exit 1; fi

echo "== writing $IMG to dtbo_a (slot a) =="
$EDL w dtbo_a "$IMG" --loader="$LOADER" --memory=ufs

echo "== readback verification =="
$EDL r dtbo_a verify_flash.bin --loader="$LOADER" --memory=ufs
a=$(sha256sum "$IMG" | cut -d' ' -f1)
b=$(sha256sum verify_flash.bin | cut -d' ' -f1)
[ "$a" = "$b" ] || { echo "READBACK MISMATCH - DO NOT RESET"; exit 1; }
echo "readback OK"

echo "== rebooting =="
# NOTE: do NOT add --memory=ufs to reset - at least one edl.py version rejects
# the combination (docopt) and the device silently never reboots.
$EDL reset --resetmode=reboot --loader="$LOADER" || true
echo "== done. If it does not reach Android within ~2 minutes, re-enter EDL =="
echo "   and reflash your stock dtbo dump the same way. =="
