# Retroid Pocket 6 CDSP/NPU Unlock

Enables the vendor-disabled CDSP (Hexagon compute DSP, the "NPU" used by
QNN/HTP and LiteRT) on the **Retroid Pocket 6** (Qualcomm QCM8550 / kailau),
on stock firmware, **without root**.

Verified working on firmware **QCM8550.LA.1.0-00035-STD.PROD-2**
(CDSP.HT.2.8-00906-KAILUA-1):

```
/sys/class/remoteproc/remoteproc3: 32300000.hexagon-npu = running fw=cdsp.mdt
fastrpc_rpmsg_probe: opened rpmsg channel for cdsp
vendor.cdsprpcd: running
```

This repo contains **no proprietary firmware** - only tools that transform
*dumps from your own device* and documentation.

> **Personal project: no warranty, no support.**
> This is a hobby project I built and documented for myself, published in
> case it's useful to someone else as-is. It is provided **without any
> warranty, express or implied**, including fitness for any purpose. I'm not
> affiliated with any vendor. I do **not** provide support: not for other
> devices or firmware versions, not for EDL setup, not for recovery. Issues
> and questions may be closed without a response. Everything here was
> verified on exactly one device on one firmware build, by one person.
> If it breaks your device, that's on you - keep your own dumps.

---

## How the lock works (short version)

Everything needed for the NPU already ships on the device: the CDSP firmware
lives in the modem partition, the PAS remoteproc driver and QNN/HTP libraries
are in the vendor tree, and TrustZone happily authenticates the DSP (proven
live). The vendor's lock is a single trick:

1. On every boot, after the bootloader merges the device tree, a component in
   the secure boot chain finds `/soc/remoteproc-cdsp@32300000` and flips its
   `status` from `"ok"` to `"no"`, so Linux never brings the DSP up.
2. The vendor's `qcom_q6v5_pas` driver additionally contains deliberate
   `panic()` calls on bring-up failure paths (defused separately, see notes).
3. The fastRPC driver gates CDSP sessions behind a "part defective" fuse bit
   (also defused separately).

## How the unlock works

We can't touch the flipper, so we go around it:

- The overlay adds **`/soc/hexagon-npu@32300000`** - a full copy of the CDSP
  node under a new name. The flipper is path-keyed and never touches the
  clone, so its `status = "ok"` survives.
- The clone carries a fresh `phandle`; the bootloader renumbers phandles in
  overlays (it adds the base tree's max phandle - on this firmware `0x999`
  lands at `0xF7E` at runtime), so the build tool bakes the post-renumber
  value in.
- The vendor's own `qcom,msm-cdsp-loader` driver is retargeted to the clone
  (`qcom,rproc-handle`), so **the vendor's own loader boots the DSP** - it
  even fetches `cdsp.mdt` + segments from the modem partition through
  ueventd's firmware fallback (`firmware_directories /vendor/firmware_mnt/image/`).
  No firmware files need to be added anywhere.

Result: the CDSP remoteproc reaches `running` at ~3.4s, the fastRPC channel
opens, `cdsprpcd` starts, and QNN/HTP delegates can create sessions.

## Requirements

- Retroid Pocket 6 on the firmware build above. Check yours matches **before**
  starting:

  ```
  adb shell getprop ro.vendor.build.fingerprint
  # expected: qti/kalama/kalama:13/TKQ1.231222.001/RP606161519:user/release-keys
  adb shell getprop ro.vendor.build.version.incremental
  # expected: eng.RP6.20260616.152218
  ```

  (Underneath that, the modem firmware metabuild is QCM8550.LA.1.0-00035-
  STD.PROD-2; that's the string the reverse-engineering notes refer to.)
  On any other build the offsets, the selected dtbo entry, and the runtime
  phandle may all differ - the tools refuse malformed input, but only the
  marker-first procedure (below) proves the phandle for *your* build.
- **Unlocked bootloader** (this is the only gate - no root needed at any point).
- A PC with Python 3.8+, [bkerler/edl](https://github.com/bkerler/edl), and a
  QCM8550 firehose programmer (not included; obtain your own).
- The RP6 does not enumerate fastboot - flashing is done in **EDL mode**
  (power off, hold Vol- + Vol+ while plugging USB, or the equivalent entry
  method for your unit).

## Read this first

- You are flashing a boot partition. A bad flash or unexpected incompatibility
  can leave the device at the boot logo; recovery is re-entering EDL and
  reflashing your stock dump. Keep your dumps. A *hard* brick is not expected
  from dtbo-only flashing, but the standard disclaimers apply: your device,
  your risk, warranty void.
- Flash the **marker image first**. It creates the new node with
  `status="no"` (completely inert to drivers) so you can verify the overlay
  pipeline and read the runtime phandle before enabling anything.
- The successful boots during development had the optional panic-defusing
  module patches flashed (see RESEARCH_NOTES). Since DSP authentication
  succeeds on this device, the dtbo-only unlock is expected to work without
  them, but that exact combination was not tested in isolation - keep EDL
  recovery ready on the first enable boot.
- **Thermal/power note:** this brings up a DSP the vendor ships disabled. That
  may be pure market segmentation, but it may also relate to the thermal or
  battery budget of a small handheld chassis. Watch temperatures under
  sustained NPU load, especially while charging or in a case.

## Procedure

```bash
# 0) dump YOUR stock partitions (keep these forever - they are your revert)
python3 edl.py r dtbo_a dtbo_a.bin --loader=firehose.elf --memory=ufs
python3 edl.py r vendor_boot_a vendor_boot_a.bin --loader=firehose.elf --memory=ufs

# 1) extract the base DTB pool and confirm index 2 is the runtime base
python3 tools/extract_dtb_pool.py --vb vendor_boot_a.bin --out dtbs/

# 2) build marker + enable images
python3 tools/build_unlock.py --dtbo dtbo_a.bin --dtb dtbs/dtb2.dtb --out out/

# 3) flash the marker, boot to Android, verify
python3 edl.py w dtbo_a out/dtbo_unlock_marker.bin --loader=firehose.elf --memory=ufs
python3 edl.py reset --resetmode=reboot --loader=firehose.elf
adb shell 'cat /proc/device-tree/soc/hexagon-npu@32300000/phandle | od -An -tx1'
adb shell 'cat /proc/device-tree/soc/hexagon-npu@32300000/npu-unlock'
# phandle must read 00 00 0f 7e. If it differs, rebuild with
#   --loader-handle <observed> before flashing enable.
# npu-unlock is just the marker string baked into the clone node by
#   build_unlock.py - reading it back proves the overlay applied, independent
#   of any phandle math (expected value: "rp6-npu-unlock").

# 4) flash the enable image the same way, boot, verify the DSP is up
adb shell 'cat /sys/class/remoteproc/remoteproc*/name; cat /sys/class/remoteproc/remoteproc*/state'
# 32300000.hexagon-npu = running
adb shell dmesg | grep -E 'hexagon-npu|cdsp\.mdt'
```

`tools/flash_unlock.sh` wraps the flash + readback-verify + reset steps.

## So the DSP is up - now what?

[docs/TESTING_THE_NPU.md](docs/TESTING_THE_NPU.md) is a field report on
running LLM inference on the unlocked Hexagon: the GenieX hybrid runtime
and the one-file fix that makes it work, measured throughput for 0.8B-9B
models on NPU-hybrid, CPU and GPU, the ~4 GB per-session mapping ceiling
and its `-ngl` workaround, the load-guard rule that prevents kernel panics,
and a gotchas checklist. Short version: the NPU is a prefill/context
accelerator, decode stays a CPU job, and the GPU is out.

## Troubleshooting (symptom and likely cause)

| Symptom | Likely cause | Fix / see |
|---|---|---|
| Frozen boot logo, never reaches adb, next good boot shows **empty pstore** | Malformed overlay - typically an unbalanced fragment; kernel unflattens the tree *before* ramoops exists, so nothing is recorded | Reflash stock or marker via EDL. RESEARCH_NOTES section 5 "logo freeze" |
| Boot-looping with dark screen, empty pstore | Pre-kernel ABL failure - overlay apply failed (unresolvable fixup / bad blob) | Same recovery; RESEARCH_NOTES section 4.1 |
| `cdsp_loader_probe: rproc not found` spam in dmesg, hexagon-npu remoteproc stays `offline` | `qcom,rproc-handle` doesn't match the runtime phandle of the clone | Re-read the phandle from the marker boot, rebuild enable with `--loader-handle` |
| No `hexagon-npu@32300000` under `/proc/device-tree/soc` at all | Overlay fragment didn't apply - wrong firmware build, or wrong dtbo entry | Confirm fingerprint above; RESEARCH_NOTES section 4.1 |
| Firmware load errors for `cdsp.mdt` in dmesg | ueventd not serving the modem FAT path (mount missing/renamed on your build) | Check `firmware_directories` in `/vendor/etc/ueventd.rc`; RESEARCH_NOTES section 4.2 |
| Flash readback MISMATCH | Interrupted/failed write - do **not** reset | Re-enter EDL, reflash; never reset after a mismatch |

## Revert

Reflash your stock dump:

```bash
python3 edl.py w dtbo_a dtbo_a.bin --loader=firehose.elf --memory=ufs
```

If you also applied the optional module patches, reflash stock
`vendor_boot_a.bin` and the original vendor_dlkm sectors the same way.

## What's in here

| Path | Purpose |
|---|---|
| `tools/build_unlock.py` | builds marker/enable dtbo images from your dumps |
| `tools/extract_dtb_pool.py` | extracts the base DTBs from your vendor_boot |
| `tools/flash_unlock.sh` | EDL flash + readback verify + reset wrapper |
| `docs/RESEARCH_NOTES.md` | full reverse-engineering writeup: the flipper hunt, ABL/ufdt source analysis, the boot-loop post-mortems, module patch details |
| `docs/TESTING_THE_NPU.md` | post-unlock field report: LLM runtimes on the HTP, backend speed results, memory ceiling, gotchas |

## Credits

- [bkerler/edl](https://github.com/bkerler/edl) for the EDL tooling.
- Qualcomm's ABL source (CodeLinaro) - the ufdt overlay analysis in the notes
  came straight from it.
- The ROCKNIX project - its device trees and firmware redistribution proved
  stock TrustZone does not gate the CDSP on this hardware family.

For your own device only. Nothing here circumvents secure boot or
DRM; it re-enables hardware the vendor ships fully provisioned but disables
in the device tree.

## AI-coded notice

This project was developed with heavy use of AI coding, using the open-source
model GLM 5.3 through the ZCode agent harness, with human direction and
review throughout. Everything here was worked out and tested on a real RP6 -
the notes are simply what actually happened on the device.

## License

MIT; see [LICENSE](LICENSE). This repo contains no code from, and is not
derived from, [bkerler/edl](https://github.com/bkerler/edl); that tool is
invoked as an external dependency and keeps its own license.

Retroid Pocket, Qualcomm, Hexagon, and related marks are trademarks of their
respective owners. This personal project is unaffiliated with and unendorsed
by any of them.
