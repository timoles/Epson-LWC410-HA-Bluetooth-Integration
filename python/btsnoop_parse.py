#!/usr/bin/env python3
"""Decode an Android btsnoop_hci.log and extract the LW-C410 ESC/P session.

Extract from a bugreport zip: FS/data/misc/bluetooth/logs/btsnoop_hci.log
(Android: developer options -> "Bluetooth HCI snoop log", reproduce, then
`adb bugreport` — or grab it from the generated bugreport folder).

What it does:
  - parses btsnoop records (H4/1002 datalink)
  - reassembles L2CAP PDUs from ACL fragments per (handle, direction)
  - decodes RFCOMM frames (SABM/UA/DISC/UIH, MCC PN/MSC, credit bytes)
  - prints the SDP query + response (SPP UUID, server channel)
  - dumps the RFCOMM data-channel (SPP) stream chronologically with timestamps,
    flagging ESC/P frames, and renders 64-byte status frames as text

Usage:
  python3 btsnoop_parse.py FS/data/misc/bluetooth/logs/btsnoop_hci.log
  python3 btsnoop_parse.py btsnoop_hci.log --data-only     # just the ESC/P stream
  python3 btsnoop_parse.py btsnoop_hci.log --dlci 12       # only one data DLCI

Python 3.9+, stdlib only.
"""
import argparse
import struct
import sys

BTSNOOP_EPOCH_OFF = 0x00E03AB2A2C00000   # us between year-0 and Unix epoch

RFCOMM_NAMES = {0x2F: "SABM", 0x63: "UA", 0x0F: "DM", 0x43: "DISC",
                0xEF: "UIH", 0xFF: "UIH-PF"}


def read_records(path):
    data = open(path, "rb").read()
    if data[:8] != b"btsnoop\x00":
        raise SystemExit(f"{path}: not a btsnoop file (got {data[:8]!r})")
    version, dlt = struct.unpack(">II", data[8:16])
    records, off = [], 16
    while off + 24 <= len(data):
        olen, ilen, flags, drops, ts = struct.unpack(">IIIIQ", data[off:off + 24])
        off += 24
        records.append((ts, flags, data[off:off + ilen]))
        off += ilen
    return records, version, dlt


class Chan:
    def __init__(self):
        self.pdus = []       # (ts, cid, payload) -- complete or growing L2CAP PDUs


def l2cap_channels(records):
    """Reassemble L2CAP PDUs per (ACL handle, direction)."""
    chans = {}
    for ts, flags, pkt in records:
        if not pkt or pkt[0] != 0x02:          # HCI ACL
            continue
        rx = bool(flags & 1)
        hf, dlen = struct.unpack("<HH", pkt[1:5])
        handle, pb = hf & 0x0FFF, (hf >> 12) & 0x3
        payload = pkt[5:5 + dlen]
        key = (handle, rx)
        if pb in (0, 2):                        # start of a new L2CAP PDU
            ch = chans.setdefault(key, Chan())
            if len(payload) >= 4:
                l2len, cid = struct.unpack("<HH", payload[:4])
                ch.pdus.append((ts, cid, payload[4:4 + l2len]))
            else:
                ch.pdus.append((ts, 0, b""))
        elif pb == 1:                           # continuation fragment
            ch = chans.get(key)
            if ch and ch.pdus:
                ts0, cid, buf = ch.pdus[-1]
                ch.pdus[-1] = (ts0, cid, buf + payload)
    return chans


def parse_rfcomm(p):
    """RFCOMM frame -> (dlci, name, payload) or None."""
    if len(p) < 3:
        return None
    addr, ctrl, lenb = p[0], p[1], p[2]
    dlci = addr >> 2
    if lenb & 1:
        ln, body_off = lenb >> 1, 3
    else:
        if len(p) < 4:
            return None
        ln, body_off = (lenb >> 1) | (p[3] << 7), 4
    name = RFCOMM_NAMES.get(ctrl & 0xEF, "ctrl_%02X" % ctrl)
    body = p[body_off:]
    if (ctrl & 0xFF) == 0xFF and body:          # UIH with P/F: credit byte first
        body = body[1:]
    return dlci, name, body[:ln]


def decode_sdp_response(p):
    """ServiceSearchAttributeResponse -> readable summary lines."""
    if len(p) < 7 or p[0] != 0x07:
        return None
    out = []
    if b"\x19\x11\x01" in p:
        out.append("service class: SerialPort (0x1101)")
    if b"\x19\x00\x03\x08" in p:           # uuid16 RFCOMM + u8 descriptor
        idx = p.index(b"\x19\x00\x03\x08")
        if idx + 4 < len(p):
            out.append(f"RFCOMM server channel {p[idx + 4]}")
    name_at = p.find(b"SerialPort")
    if name_at >= 0:
        out.append('service name: "SerialPort"')
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("logfile")
    ap.add_argument("--data-only", action="store_true",
                    help="only the RFCOMM data-channel (ESC/P) stream")
    ap.add_argument("--dlci", type=int, default=None,
                    help="restrict the data stream to this DLCI")
    args = ap.parse_args(argv)

    records, version, dlt = read_records(args.logfile)
    t0 = records[0][0] if records else 0
    chans = l2cap_channels(records)

    if not args.data_only:
        print(f"btsnoop v{version} datalink {dlt}: {len(records)} records, "
              f"span {(records[-1][0] - t0) / 1e6:.2f} s")
        print("\n=== SDP exchange (SPP lookup) ===")
        for (handle, rx), ch in sorted(chans.items()):
            for ts, cid, p in ch.pdus:
                if p[:1] in (b"\x05", b"\x06") and b"\x19\x11\x01" in p[:12]:
                    print(f"[+{(ts - t0) / 1e6:8.3f}] h{handle:04X} "
                          f"{'RX' if rx else 'TX'} cid {cid:04X} "
                          "ServiceSearchAttribute(UUID 1101)")
                lines = decode_sdp_response(p)
                if lines:
                    print(f"[+{(ts - t0) / 1e6:8.3f}] h{handle:04X} "
                          f"{'RX' if rx else 'TX'} cid {cid:04X} "
                          "SDP response: " + "; ".join(lines))

    print("\n=== RFCOMM stream (chronological) ===")
    events = []
    for (handle, rx), ch in chans.items():
        for ts, cid, p in ch.pdus:
            if cid < 0x0040:                    # signaling / fixed channels
                continue
            r = parse_rfcomm(p)
            if r:
                events.append((ts, handle, rx, cid) + r)
            else:
                events.append((ts, handle, rx, cid, None, "L2CAP?", p))
    events.sort(key=lambda e: e[0])
    for ts, handle, rx, cid, dlci, name, payload in events:
        if args.dlci is not None and dlci is not None and dlci != args.dlci:
            continue
        d = "RX <==" if rx else "TX ==>"
        dlci_s = f"DLCI {dlci:<2}" if dlci is not None else "       "
        line = (f"[+{(ts - t0) / 1e6:8.3f}] h{handle:04X} {d} cid {cid:04X} "
                f"{dlci_s} {name:<6} len={len(payload):5d}")
        if name in ("UIH", "UIH-PF") and dlci not in (None, 0) and payload:
            if payload[0:1] == b"\x1B":
                line += "  ESC/P"
            if len(payload) == 64 and payload[0] == 0x40 and payload[63] == 0xFF:
                body = payload[1:63].decode("latin-1")
                line += "\n             STATUS | " + body.replace("\r", " ").replace("\n", " ")
                continue
            head = " ".join("%02X" % b for b in payload[:24])
            tail = (" .. " + " ".join("%02X" % b for b in payload[-12:])
                    if len(payload) > 40 else "")
            line += f"  {head}{tail}"
        elif payload and len(payload) <= 12:
            line += "  " + payload.hex()
        print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
