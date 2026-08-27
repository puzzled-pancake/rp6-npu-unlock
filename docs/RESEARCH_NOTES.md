# RP6 CDSP/NPU Unlock: Research Notes

Full writeup of how the vendor lock on the Retroid Pocket 6 (QCM8550/kailau,
Android 13 user build) was found and bypassed. Outcome: CDSP remoteproc
reaches `running` with stock firmware, stock bootloader, stock TrustZone and
hypervisor - no root.

## 1. What the device ships with (all verified from dumps)

- **CDSP firmware exists**: modem FAT `/image/` holds `cdsp.mdt` + b00-b15 +
  `cdsp_dtb.*` (CDSP.HT.2.8-00906-KAILUA-1, variant `kailau.cdsp.prodQ`,
  built from QCM8550_R01_BA01_r035). The DSP image contains the full AI stack:
  FastRPC HMX (tensor core) and HVX clients, streamer clients, an embedded
  Caffe NN runtime, `qProcessList;cpu:Hexagonv73`.
- **Host stack exists**: `libQnnHtp.so`, `libQnnHtpV73Skel.so` (DSP-side QNN
  skeleton), `libcdsprpc.so`, `libcdsp_default_listener.so`, `cdsprpcd` in
  /vendor.
- **XBL is provisioned** to load/authenticate CDSP (`[CORE_CDSP]` config,
  PIL proxy voting for subsys 18/37).
- **Base DTBs ship the node enabled**: all 10 vendor_boot DTBs contain
  `/soc/remoteproc-cdsp@32300000` with `status="ok"`.
- The metabuild is stock Qualcomm QCM8550 reference
  (QCM8550.LA.1.0-00035-STD.PROD-2), not a Retroid custom firmware set.

## 2. The lock, precisely

1. **The flipper**: between the ABL device-tree merge and kernel start, the
   original CDSP node's `status` is changed from `ok` to `no`. Kernel then never
   probes it. Evidence it is path/name-keyed: a clone node with identical
   `compatible` but a different name is left untouched.
2. **Panic-by-design**: `qcom_q6v5_pas.ko` calls `panic()` on DSP bring-up
   failure paths ("Panicking, auth and reset failed for remoteproc %s" via
   `adsp_panic` in `qcom_q6v5.ko`, plus 6 direct `bl panic` sites). If you
   naively force the node on and bring-up fails, the phone boot-loops by
   design.
3. **fastRPC fuse gate**: `frpc-adsprpc.ko` checks `socinfo
   subset_parts` PART_COMP bit and refuses CDSP sessions ("part defective").
   A 1-instruction patch makes `fastrpc_get_cdsp_status()` return 1.

Where the flipper is NOT (elimination matrix): ABL (no status prop strings),
XBL, kernel image, all vendor_dlkm + ramdisk modules (no of_update_property
importers), socinfo, featenabler/devcfg/imagefv/cpucp/xbl_config. Remaining
unscannable candidates: hyp (Gunyah) / TZ - and the hypervisor delivers its
own device-tree overlays *after* the dtbo partition overlay
(HypDtboBaseAddr in ABL), which is the delivery mechanism that outranks
anything in dtbo. We never needed to identify it exactly, because the clone
node sidesteps it entirely.

## 3. TrustZone does not gate the CDSP (proven twice)

- **Indirect**: ROCKNIX ships the *same CDSP firmware build* for SM8550
  Odin2/RP6-class handhelds - same size (7,086,504 bytes), same
  QC_IMAGE_VERSION_STRING and variant string, 99.84% identical (11,113 bytes
  differ, clustered in per-build signature/cert blocks) - booted via PAS
  through the vendor's own TZ on those devices.
- **Direct**: our enable boot passed PIL authentication on this device -
  `remote processor 32300000.hexagon-npu is now up`.

## 4. The unlock (final recipe)

### 4.1 Overlay mechanics on this ABL

- dtbo partition: QTI table (magic 0xd7b7ab1e), 56 entries x 32 B at 0x20.
  ABL walks all entries (`fdt_check_header` on each; a broken blob *before*
  the matching entry aborts the scan), selects the rank-best board match -
  entry 51 (376,193-byte blob) on RP6 - then merges base DTB (dtb2 from the
  vendor_boot pool) + entry-51 blob + hypervisor overlays with
  `ufdt_apply_multi_overlay`.
- A rebuilt entry blob may live **beyond the stock total_size** - ABL loads
  the whole 24 MiB partition and only checks `0 < total_size <= 24 MiB`
  (verified) - so we park it at `stock_total+1`, rewrite the header's
  total_size to cover it, and repoint entry 51.
- Fragment rules learned from ABL source plus experiments:
  - `target-path` at the **fragment level** works and needs no fixups; a
    `target` prop referenced by `__fixups__` must resolve or the entire
    multi-overlay apply fails - ABL "tolerates" that failure but then aborts
    before the kernel (UpdateDeviceTree's header check), producing a dark
    boot-loop with an **empty pstore** (kernel never ran).
  - Only `MERGE_FAIL` is fatal in fragment apply; missing targets are
    silently skipped.
  - Leave stock `__fixups__`/`__local_fixups__` byte-identical.
  - **Keep the stock struct block byte-verbatim and splice** - don't
    re-emit. (Non-zero padding garbage in stock blobs is don't-care, but
    re-emitting bought us nothing and debugging cost.)
  - **Endnode-balance your fragments.** An unbalanced fragment produces a
    tree the kernel's unflatten walks at ~0.05 s - *before* ramoops init
    (0.083 s) - so you get a frozen logo, no adb, and an empty pstore that
    looks exactly like a bootloader failure. (This cost a full bisect
    against the wrong suspect.)

### 4.2 The two fragments

```
fragment@63 {
    target-path = "/soc";
    __overlay__ {
        hexagon-npu@32300000 {      /* full copy of the base CDSP subtree */
            /* ...all props except phandle/status... */
            phandle = <0x999>;      /* ABL renumbers: 0x999 -> 0xf7e at runtime */
            npu-unlock = "...";
            status = "no";          /* "ok" in the enable image */
        };
    };
};
fragment@64 {
    target-path = "/soc/qcom,msm-cdsp-loader";
    __overlay__ {
        qcom,rproc-handle = <0xf7e>;   /* = post-renumber phandle of the clone */
    };
};
```

Why the phandle dance: ABL's ufdt bumps every phandle defined inside an
overlay by the base tree's max phandle, but does **not** adjust plain
phandle-valued props that aren't declared in `__local_fixups__`. Rather than
editing `__local_fixups__` (stock, fragile), we bake the post-bump value into
the loader fragment. The bump delta measured on-device was 0x5e5 (not the
0x5e8 a naive max-phandle scan of dtb2 predicts) - always confirm the runtime
value with a marker boot before flashing enable.

Boot flow after enable (observed timings): the loader stops logging "rproc
not found" once our rproc registers (~1.4 s), then comes `powering up
32300000.hexagon-npu` (3.13 s), loading of `cdsp.mdt` + segments +
`cdsp_dtb.*` via the kernel sysfs fallback served by ueventd from
`firmware_directories /vendor/firmware_mnt/image/` (the modem FAT mount),
then PIL auth through TZ, `remote processor ... is now up` (3.42 s), the
fastrpc rpmsg channel for cdsp opening (3.43 s), and finally `cdsprpcd`
starting.

### Final verification (session transcript, 2026-08-26)

The final marker and enable boots were verified with ad-hoc adb reads; their
outputs are transcribed here verbatim (the raw shell sessions were not
archived to disk at the time, which is why they appear nowhere else).

Marker boot (`dtbo_v13fix_marker.bin`, status="no" - inert):
```
$ adb shell 'cat /proc/device-tree/soc/hexagon-npu@32300000/phandle | od -An -tx1'
 00 00 0f 7e                      <- runtime phandle: 0xf7e (raw 0x999 + 0x5e5)
$ adb shell 'cat /proc/device-tree/soc/hexagon-npu@32300000/status' ; > "no"
  (original node still flipped to "no"; clone untouched)
```

Enable boot (`dtbo_v13fix2_enable.bin` - the shipped image):
```
$ adb shell 'for r in /sys/class/remoteproc/remoteproc*; do echo "$r: $(cat $r/name) = $(cat $r/state) fw=$(cat $r/firmware)"; done'
 /sys/class/remoteproc/remoteproc0: 188101c.remoteproc-spss  = attached fw=unknown
 /sys/class/remoteproc/remoteproc1: 3000000.remoteproc-adsp   = running fw=adsp.mdt
 /sys/class/remoteproc/remoteproc2: 4080000.remoteproc-mss    = offline fw=modem.mdt
 /sys/class/remoteproc/remoteproc3: 32300000.hexagon-npu      = running fw=cdsp.mdt

$ adb shell dmesg | grep -E 'hexagon-npu|cdsp\.mdt'
 [    3.128939] remoteproc remoteproc3: powering up 32300000.hexagon-npu
 [    3.130468] remoteproc remoteproc3: Booting fw image cdsp.mdt, size 5084
 [    3.139254] adsprpc: fastrpc_restart_notifier_cb: subsystem cdsp is about to start
 [    3.418750] adsprpc: fastrpc_restart_notifier_cb: cdsp subsystem is up
 [    3.418755] remoteproc remoteproc3: remote processor 32300000.hexagon-npu is now up
 [    3.425774] fastrpc_rpmsg_probe: opened rpmsg channel for cdsp
 [   15.779459] adsprpc : fastrpc_cdsp_status : 1

$ adb shell getprop init.svc.vendor.cdsprpcd ; > [running]
```
(The remoteproc index of hexagon-npu varies between boots - 2 or 3 depending
on registration order; match on the name, not the index.)

### 4.3 Optional: panic-defusing module patches (safety net)

Patched `adsp_panic` (first instruction > `ret`) in both copies of
`qcom_q6v5.ko`/`qcom_q6v5_pas.ko` (vendor_boot first-stage ramdisk +
vendor_dlkm). Boot-tested inert on normal boots; if DSP bring-up ever fails
they turn a designed boot-loop into a normal boot with an error in dmesg.
The 6 direct `bl panic` sites in `q6v5_pas` were catalogued but never needed
(auth succeeded). Binary offsets are firmware-specific - re-derive if the
build differs. Caveat: every successful boot in this project had these
patches flashed; the dtbo-only combination without them was **not** tested in
isolation (authentication succeeded, so the panic paths should never fire -
but that inference is untested).

### 4.4 Optional: fastRPC fuse patch

1-instruction patch to `frpc-adsprpc.ko` so `fastrpc_get_cdsp_status()`
returns 1 regardless of the PART_COMP fuse bit (dmesg then shows
`fastrpc_get_cdsp_status: cdsp available with status: 1`). Needed for
userspace sessions; also firmware-version-specific.

## 5. Incident log (what each failure taught)

- **reset with `--memory=ufs`**: docopt silently rejects it; device never
  rebooted > bogus "no boot" verdicts. `reset` takes no `--memory`.
- **vendor_boot v4 ramdisk rebuild**: `vendor_ramdisk_size` counts the LZ4
  legacy magic (blocks + 4), and `lz4.block.compress(..., mode='high_compression',
  compression=12, store_size=False)` reproduces stock-ish sizes that fit the
  ~3.5 KB slack. A 4-byte truncation of the ramdisk = kernel never decompresses.
- **Wrong source blob**: rebuilding entry 51 from entry 49's blob (different
  board variant) never matches in ABL's board scan > hard EFI_NOT_FOUND
  abort. Always splice *into the entry ABL actually selects*.
- **`target` prop at the wrong level** (`__overlay__/target` vs fragment
  level with a fixup pointing at fragment level): ufdt fixup resolution
  fails > whole apply fails > tolerated-fatal chain (empty pstore loop).
- **Marker placement**: fragments are applied in tree order; our fragment
  appended last works (the "must be first" theory from early experiments was
  a red herring caused by the malformed early builders).
- **The logo freeze** (see 4.1): unbalanced fragment, kernel unflatten walks
  it before ramoops exists (ramoops registered at 0.084 s on this kernel;
  unflatten runs earlier). Symptom triple: frozen logo, no adb ever, empty
  pstore. If you ever see that triple, suspect your own blob first.

## 6. Open items

- The flipper's exact identity (hyp dtbo vs. memory patch) remains
  unidentified - unnecessary for the unlock, interesting for science.
- `remoteproc3/recovery` is `disabled`: if the CDSP ever crashes it stays
  down until rebooted (or toggled with root).
- OTA updates to dtbo/vendor_boot require rebuilding (the tools take your
  fresh dumps; the phandle value must be re-verified with a marker boot).
