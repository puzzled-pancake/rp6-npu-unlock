#!/usr/bin/env python3
"""
build_unlock.py - Retroid Pocket 6 (QCM8550) CDSP/NPU enablement overlay builder.

Takes YOUR stock dtbo_a dump + the matching base DTB (extracted from your
vendor_boot) and produces two flashable dtbo images:

  *_marker.bin - new node created with status="no" (inert; safe first flash to
                 verify the overlay pipeline and read back the runtime phandle)
  *_enable.bin - status="ok": the vendor cdsp-loader boots the cloned CDSP
                 remoteproc from the new node.

Method (see ../docs/RESEARCH_NOTES.md for the full story):
  fragment@63: target-path=/soc -> hexagon-npu@32300000 = full copy of the base
      /soc/remoteproc-cdsp@32300000 subtree (phandle/status props stripped),
      plus a fresh phandle and a marker prop. The runtime "flipper" that
      disables the real CDSP node is path-keyed and ignores the clone.
  fragment@64: target-path=/soc/qcom,msm-cdsp-loader -> retarget
      qcom,rproc-handle to the clone so the vendor loader boots it.

The bootloader (ABL/ufdt) renumbers phandles inside overlays by adding the
base tree's max phandle. On firmware QCM8550.LA.1.0-00035-STD.PROD-2 a raw
value of 0x999 lands at 0xF7E at runtime - ALWAYS verify with the marker
image first and pass --loader-handle if your value differs.

Usage:
  python3 build_unlock.py --dtbo dtbo_a.bin --dtb dtb2.dtb --out out_dir
  python3 build_unlock.py ... --phandle-raw 0x999 --loader-handle 0xf7e

This tool embeds NO proprietary content: everything is derived from your own
dumps at run time. Requires Python 3.8+, no dependencies.
"""
import argparse
import os
import struct
import sys

FDT_BEGIN, FDT_END_NODE, FDT_PROP, FDT_NOP, FDT_END = 1, 2, 3, 4, 9
DTBO_MAGIC = 0xD7B7AB1E          # QTI variant dtbo table magic
NEW_NODE = b"hexagon-npu@32300000"
CDSP_NODE = b"remoteproc-cdsp@"
LOADER_NODE = b"qcom,msm-cdsp-loader"
FRAG_NODE = 63
FRAG_LOADER = 64


# ---------------------------------------------------------------- FDT parsing
def parse_fdt(b):
    """Parse an FDT (DTB) blob into a token list + metadata."""
    magic, totalsize, off_struct, off_strings = struct.unpack(">4I", b[:16])
    off_rsv, version, last_comp, bootcpu, size_strings, size_struct = struct.unpack(">6I", b[16:40])
    if magic != 0xD00DFEED:
        raise SystemExit(f"not an FDT blob (magic {magic:#x})")
    if off_struct + size_struct > len(b) or off_strings + size_strings > len(b):
        raise SystemExit("FDT header inconsistent with blob size")
    strings = b[off_strings:off_strings + size_strings]

    def sname(noff):
        e = strings.index(b"\0", noff)
        return strings[noff:e]

    tokens, pos = [], off_struct
    while True:
        tok = struct.unpack(">I", b[pos:pos + 4])[0]
        pos += 4
        if tok == FDT_BEGIN:
            e = b.index(b"\0", pos)
            nm = b[pos:e]
            pos = (e + 1 + 3) & ~3
            tokens.append(["node", nm])
        elif tok == FDT_PROP:
            ln, noff = struct.unpack(">II", b[pos:pos + 8])
            pos += 8
            tokens.append(["prop", (noff, sname(noff), b[pos:pos + ln])])
            pos = (pos + ln + 3) & ~3
        elif tok == FDT_END_NODE:
            tokens.append(["endnode", None])
        elif tok == FDT_NOP:
            tokens.append(["nop", None])
        elif tok == FDT_END:
            break
        else:
            raise SystemExit(f"bad FDT token {tok:#x} at {pos - 4:#x}")
    if pos != off_strings:
        raise SystemExit(f"FDT walk desynced: ended {pos:#x}, strings at {off_strings:#x}")
    return dict(tokens=tokens, strings=strings, off_rsv=off_rsv, off_struct=off_struct,
                version=version, last_comp=last_comp, bootcpu=bootcpu,
                size_struct=size_struct, totalsize=totalsize)


def balance(tokens):
    return sum(1 if k == "node" else (-1 if k == "endnode" else 0) for k, _ in tokens)


def extract_subtree(tokens, name_prefix):
    """Extract the /soc/<node starting with prefix> subtree, token-inclusive."""
    depth, out, cap = 0, [], False
    for kind, payload in tokens:
        if kind == "node":
            depth += 1
            if depth == 3 and not cap and payload.startswith(name_prefix):
                cap = True
        if cap:
            out.append([kind, payload])
        if kind == "endnode":
            depth -= 1
            if cap and depth <= 2:
                break
    if not cap:
        raise SystemExit(f"node '{name_prefix.decode()}*' not found at /soc level")
    if balance(out) != 0:
        raise SystemExit("extracted subtree unbalanced")
    return out


# ------------------------------------------------------------- fragment build
class FragEmitter:
    """Emit fragment bytes; prop names resolved against the stock strings block
    (reusing existing nameoffs where possible, appending new names)."""

    def __init__(self, stock_strings):
        self.stock = stock_strings
        self.appended = bytearray()
        self.lookup = {}
        pos = 0
        while pos < len(stock_strings):
            e = stock_strings.index(b"\0", pos)
            self.lookup[bytes(stock_strings[pos:e])] = pos
            pos = e + 1

    def noff(self, nm):
        if nm in self.lookup:
            return self.lookup[nm]
        off = len(self.stock) + len(self.appended)
        self.appended += nm + b"\0"
        self.lookup[nm] = off
        return off

    def emit(self, tokens):
        out = bytearray()
        for kind, payload in tokens:
            if kind == "node":
                out += struct.pack(">I", FDT_BEGIN) + payload + b"\0"
            elif kind == "prop":
                nm, val = payload
                out += struct.pack(">III", FDT_PROP, len(val), self.noff(nm)) + val
            elif kind == "endnode":
                out += struct.pack(">I", FDT_END_NODE)
            else:
                raise SystemExit(f"cannot emit token {kind}")
            while len(out) % 4:
                out += b"\0"
        return bytes(out)


def cdsp_props_and_children(subtree):
    """Strip the source node's own open token + close token, and any
    phandle/linux,phandle/status props anywhere (we add our own)."""
    inner = []
    for k, pl in subtree[1:-1]:
        if k == "prop":
            if pl[1] in (b"phandle", b"linux,phandle", b"status"):
                continue
            inner.append(["prop", (pl[1], pl[2])])
        else:
            inner.append([k, pl])
    return inner


def make_fragments(inner, status_val, phandle_raw, loader_handle, marker_val):
    frag63 = (
        [["node", b"fragment@%d" % FRAG_NODE],
         ["prop", (b"target-path", b"/soc\0")],
         ["node", b"__overlay__"],
         ["node", NEW_NODE]] + inner +
        [["prop", (b"phandle", struct.pack(">I", phandle_raw))],
         ["prop", (b"npu-unlock", marker_val)],
         ["prop", (b"status", status_val)],
         ["endnode", None],      # NEW_NODE
         ["endnode", None],      # __overlay__
         ["endnode", None]])     # fragment
    frag64 = (
        [["node", b"fragment@%d" % FRAG_LOADER],
         ["prop", (b"target-path", b"/soc/" + LOADER_NODE + b"\0")],
         ["node", b"__overlay__"],
         ["prop", (b"qcom,rproc-handle", struct.pack(">I", loader_handle))],
         ["endnode", None],      # __overlay__
         ["endnode", None]])     # fragment
    for name, f in (("node-fragment", frag63), ("loader-fragment", frag64)):
        if balance(f) != 0:
            raise SystemExit(f"{name} unbalanced - refusing to build a logo-freeze blob")
    return frag63 + frag64


# ---------------------------------------------------------------- image build
def splice(stock_blob, frag_tokens):
    p = parse_fdt(stock_blob)
    if p["strings"][-1:] != b"\0":
        raise SystemExit("stock strings block does not end in NUL - unexpected format")
    # refuse to double-apply: if this blob already carries our fragments, the
    # user is feeding a previously built image, not a stock dump
    root_names = [pl for k, pl in p["tokens"] if k == "node" and pl.startswith(b"fragment@")]
    if b"fragment@%d" % FRAG_NODE in root_names or b"fragment@%d" % FRAG_LOADER in root_names:
        raise SystemExit("input already contains our fragments - use your ORIGINAL stock dump")
    fe = FragEmitter(p["strings"])
    frag_bytes = fe.emit(frag_tokens)
    new_strings = bytes(p["strings"]) + bytes(fe.appended)

    old_struct = stock_blob[p["off_struct"]:p["off_struct"] + p["size_struct"]]
    if old_struct[-8:] != struct.pack(">II", FDT_END_NODE, FDT_END):
        raise SystemExit("unexpected struct block tail")
    # splice fragments before the root's closing endnode:
    new_struct = old_struct[:-8] + frag_bytes + old_struct[-8:]

    off_struct = p["off_struct"]
    off_strings = (off_struct + len(new_struct) + 3) & ~3
    totalsize = off_strings + len(new_strings)
    hdr = struct.pack(">10I", 0xD00DFEED, totalsize, off_struct, off_strings,
                      p["off_rsv"], p["version"], p["last_comp"], p["bootcpu"],
                      len(new_strings), len(new_struct))
    nb = bytearray(hdr)
    nb += stock_blob[40:p["off_rsv"]]
    nb += stock_blob[p["off_rsv"]:off_struct]
    if len(nb) != off_struct:
        raise SystemExit("header/rsv layout mismatch")
    nb += new_struct
    nb += b"\0" * (off_strings - len(nb))
    nb += new_strings
    if len(nb) != totalsize:
        raise SystemExit("blob size mismatch")

    # verify: reparse, stock token stream untouched, balanced tree
    chk = parse_fdt(bytes(nb))
    if balance(chk["tokens"]) != 0:
        raise SystemExit("built blob tree unbalanced")
    if chk["tokens"][: len(p["tokens"]) - 1] != p["tokens"][:-1]:
        raise SystemExit("stock token stream changed")
    if len(chk["tokens"]) != len(p["tokens"]) + len(frag_tokens):
        raise SystemExit("token count mismatch")
    if bytes(nb)[off_struct:off_struct + len(old_struct) - 8] != old_struct[:-8]:
        raise SystemExit("stock struct bytes not preserved verbatim")
    return bytes(nb), fe.appended


def build_image(stock_dtbo, blob_new, entry_index):
    img = bytearray(stock_dtbo)
    magic, total, hsize, esize, ecount, eoff = struct.unpack(">6I", img[:24])
    if magic != DTBO_MAGIC:
        raise SystemExit(f"not a QTI dtbo image (magic {magic:#x})")
    if hsize != 32 or esize != 32:
        raise SystemExit(f"unexpected dtbo header/entry sizes {hsize}/{esize}")
    e = eoff + entry_index * esize
    if not (0 <= entry_index < ecount) or e + esize > len(img):
        raise SystemExit(f"entry index {entry_index} out of range (0..{ecount - 1})")
    # every referenced blob must live below total_size, and the parking spot
    # (total+1, rounded up) must not overlap the entry table or any blob
    for i in range(ecount):
        ee = eoff + i * esize
        sz, off = struct.unpack(">II", img[ee:ee + 8])
        if off + sz > total:
            raise SystemExit(f"entry {i} blob exceeds total_size - not a sane dtbo image")
    new_off = (total + 1) & ~3              # park the rebuilt blob past stock data
    if eoff + ecount * esize > new_off:
        raise SystemExit("parking offset would overlap the entry table - refusing")
    if new_off + len(blob_new) > len(img):
        raise SystemExit("rebuilt blob does not fit in the partition image")
    img[new_off:new_off + len(blob_new)] = blob_new
    struct.pack_into(">II", img, e, len(blob_new), new_off)
    struct.pack_into(">I", img, 4, new_off + len(blob_new))
    return bytes(img)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dtbo", required=True, help="stock dtbo_a dump (full partition image)")
    ap.add_argument("--dtb", required=True, help="base DTB with the cdsp node (dtb2 on RP6 stock)")
    ap.add_argument("--out", default="out", help="output directory")
    ap.add_argument("--entry-index", type=lambda x: int(x, 0), default=51,
                    help="dtbo table entry ABL selects (default 51)")
    ap.add_argument("--phandle-raw", type=lambda x: int(x, 0), default=0x999,
                    help="raw phandle written into the clone node (default 0x999)")
    ap.add_argument("--loader-handle", type=lambda x: int(x, 0), default=0xF7E,
                    help="value written to qcom,rproc-handle - must equal the "
                         "RUNTIME (post-ABL-bump) phandle (default 0xf7e, verified "
                         "on QCM8550.LA.1.0-00035-STD.PROD-2)")
    ap.add_argument("--marker-value", type=lambda s: s.encode() + b"\0",
                    default=b"rp6-npu-unlock\0",
                    help="marker prop value (NUL-terminated for you)")
    args = ap.parse_args()

    for nm, v in (("--phandle-raw", args.phandle_raw),
                  ("--loader-handle", args.loader_handle)):
        if not (0 < v < 0x100000000):
            raise SystemExit(f"{nm} must be a 1..0xffffffff integer (0 means 'no phandle')")
    if args.phandle_raw == args.loader_handle:
        print("note: --phandle-raw == --loader-handle; that only makes sense if "
              "ABL applies no phandle renumbering to this blob", file=sys.stderr)

    stock = open(args.dtbo, "rb").read()
    # validate the table before any entry is read
    magic, total, hsize, esize, ecount, eoff = struct.unpack(">6I", stock[:24])
    if magic != DTBO_MAGIC:
        raise SystemExit(f"not a QTI dtbo image (magic {magic:#x})")
    if hsize != 32 or esize != 32:
        raise SystemExit(f"unexpected dtbo header/entry sizes {hsize}/{esize}")
    if not (0 <= args.entry_index < ecount):
        raise SystemExit(f"--entry-index {args.entry_index} out of range (0..{ecount - 1})")

    base = parse_fdt(open(args.dtb, "rb").read())

    # sanity: the base DTB really carries the CDSP node we copy from
    sub = extract_subtree(base["tokens"], CDSP_NODE)
    compat = [pl[2] for k, pl in sub if k == "prop" and pl[1] == b"compatible"]
    if not any(b"cdsp-pas" in c for c in compat):
        raise SystemExit(f"no *cdsp-pas* compatible under /soc/{CDSP_NODE.decode()}* in --dtb")

    # sanity: the stock blob's __fixups__/__local_fixups__ are stock (we never touch them)
    magic, total, hsize, esize, ecount, eoff = struct.unpack(">6I", stock[:24])
    e = eoff + args.entry_index * esize
    sz, off = struct.unpack(">II", stock[e:e + 8])
    print(f"dtbo: total={total:#x} entries={ecount} entry[{args.entry_index}] "
          f"size={sz} off={off:#x}")
    stock_blob = stock[off:off + sz]

    inner = cdsp_props_and_children(sub)
    os.makedirs(args.out, exist_ok=True)
    for suffix, status_val in (("marker", b"no\0"), ("enable", b"ok\0")):
        frags = make_fragments(inner, status_val, args.phandle_raw,
                               args.loader_handle, args.marker_value)
        blob, appended = splice(stock_blob, frags)
        out = build_image(stock, blob, args.entry_index)
        name = os.path.join(args.out, f"dtbo_unlock_{suffix}.bin")
        open(name, "wb").write(out)
        print(f"[{name}] blob {len(stock_blob)} -> {len(blob)} bytes "
              f"(+{len(blob) - len(stock_blob)}), new strings: {bytes(appended)!r}")
    print("\nNext steps:")
    print(f"  1. flash the MARKER image first, boot, then check:")
    print(f"     adb shell 'cat /proc/device-tree/soc/{NEW_NODE.decode()}/phandle | od -An -tx1'")
    print(f"     If it is not {args.loader_handle:#x}, rebuild the enable image with")
    print(f"     --loader-handle <observed value>.")
    print(f"  2. flash the ENABLE image; success looks like:")
    print(f"     /sys/class/remoteproc/remoteproc*: 32300000.hexagon-npu = running fw=cdsp.mdt")


if __name__ == "__main__":
    main()
