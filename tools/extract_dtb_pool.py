#!/usr/bin/env python3
"""
extract_dtb_pool.py - extract the base DTB pool from a Retroid Pocket 6
(QCM8550) vendor_boot image.

vendor_boot v4 (QTI layout):
  magic(8) ver@8 page_size@0x0C kernel_addr@0x10 ramdisk_addr@0x14
  vendor_ramdisk_size@0x18 cmdline@0x1C(2048) tags@0x81C name@0x820
  header_size@0x830 dtb_size@0x834 dtb_addr@0x838
  [header][pad to page] ramdisk [pad to page] DTB pool (10 concatenated DTBs)

ABL picks ONE of these as the runtime base tree (rank-best board match).
On RP6 stock firmware (QCM8550.LA.1.0-00035) that is index 2 - verify on a
stock boot with:
  adb shell 'cat /proc/device-tree/soc/remoteproc-cdsp@32300000/reg | od -An -tx1'
and compare against the per-index output below before using one as the source
for the unlock overlay.

Usage: python3 extract_dtb_pool.py --vb vendor_boot_a.bin --out dtbs/
"""
import argparse
import os
import struct

FDT_MAGIC = b"\xd0\x0d\xfe\xed"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--vb", required=True, help="stock vendor_boot_a dump")
    ap.add_argument("--out", default="dtbs", help="output directory")
    args = ap.parse_args()

    data = open(args.vb, "rb").read()
    if data[:4] != b"VNDR":
        raise SystemExit("not a vendor_boot image (magic != VNDR)")
    page = struct.unpack_from("<I", data, 0x0C)[0]
    vrs = struct.unpack_from("<I", data, 0x18)[0]
    hsize = struct.unpack_from("<I", data, 0x830)[0]
    dtb_size = struct.unpack_from("<I", data, 0x834)[0]

    ramdisk_start = ((hsize + page - 1) // page) * page
    dtb_start = ((ramdisk_start + vrs + page - 1) // page) * page
    print(f"page={page} vendor_ramdisk_size={vrs} dtb_size={dtb_size} "
          f"dtb pool @ {dtb_start:#x}")

    sec = data[dtb_start:dtb_start + dtb_size]
    os.makedirs(args.out, exist_ok=True)
    idx, pos = 0, 0
    while pos + 8 <= len(sec):
        if sec[pos:pos + 4] != FDT_MAGIC:
            # not a DTB here - stop, but say so (a padded pool would land here)
            break
        totalsize = struct.unpack_from(">I", sec, pos + 4)[0]
        if totalsize < 40 or pos + totalsize > len(sec):
            raise SystemExit(f"dtb{idx}: corrupt totalsize {totalsize} at {pos:#x}")
        blob = sec[pos:pos + totalsize]
        name = os.path.join(args.out, f"dtb{idx}.dtb")
        open(name, "wb").write(blob)
        print(f"  dtb{idx}.dtb  off={pos:#x} size={totalsize}")
        idx += 1
        pos += totalsize
    if pos < len(sec) - 3 and idx:
        print(f"warning: {len(sec) - pos} trailing bytes after dtb{idx - 1} - "
              f"pool may contain padding or missed DTBs")
    print(f"\nextracted {idx} DTBs to {args.out}/")
    print("RP6 stock firmware uses dtb2 (549361 bytes) as the runtime base tree.")


if __name__ == "__main__":
    main()
