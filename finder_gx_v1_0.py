#!/usr/bin/env python3
"""Create and validate FlashForge Finder xgcode 1.0 (.gx) files.

The Finder GX container is a 58-byte metadata header, a 14,454-byte
80x60 24-bit BMP preview, then ordinary text G-code.

No third-party packages are required.
"""
from __future__ import annotations

import argparse
import re
import struct
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

VERSION = "1.0.0"
GX_VERSION = b"xgcode 1.0\n\x00"
BITMAP_START = 58
GCODE_START = 14512
BITMAP_SIZE = GCODE_START - BITMAP_START
WIDTH = 80
HEIGHT = 60


class GXError(ValueError):
    pass


@dataclass
class GXMetadata:
    print_time: int = 0
    filament_usage: int = 0
    filament_usage_left: int = 0
    multi_extruder_type: int = 5
    layer_height_microns: int = 0
    shells: int = 0
    print_speed: int = 0
    bed_temperature: int = 0
    extruder_temperature: int = 0
    extruder_temperature_left: int = 0
    final_field: int = -2


def _clamp_int(value: float | int, lo: int, hi: int) -> int:
    return max(lo, min(hi, int(round(value))))


def _first_number(text: str, patterns: list[str]) -> float | None:
    for pattern in patterns:
        matches = re.findall(pattern, text, flags=re.IGNORECASE | re.MULTILINE)
        if not matches:
            continue
        value = matches[-1]
        if isinstance(value, tuple):
            value = next((v for v in value if v != ""), "")
        m = re.search(r"[-+]?\d+(?:\.\d+)?", str(value))
        if m:
            return float(m.group(0))
    return None


def _duration_seconds(value: str) -> int:
    value = value.strip()
    if re.fullmatch(r"\d+(?:\.\d+)?", value):
        return int(float(value))
    total = 0
    for amount, unit in re.findall(r"(\d+(?:\.\d+)?)\s*([dhms])", value, flags=re.IGNORECASE):
        total += int(float(amount) * {"d": 86400, "h": 3600, "m": 60, "s": 1}[unit.lower()])
    return total


def parse_metadata(gcode: bytes) -> GXMetadata:
    text = gcode.decode("utf-8-sig", "replace")
    md = GXMetadata()

    time_match = re.findall(
        r"^;\s*estimated printing time[^=]*=\s*([^\r\n]+)",
        text,
        flags=re.IGNORECASE | re.MULTILINE,
    )
    if time_match:
        md.print_time = _clamp_int(_duration_seconds(time_match[-1]), 0, 0xFFFFFFFF)
    else:
        time_value = _first_number(text, [r"^;\s*TIME\s*:\s*(\d+)"])
        if time_value is not None:
            md.print_time = _clamp_int(time_value, 0, 0xFFFFFFFF)

    filament = _first_number(text, [
        r"^;\s*filament used\s*\[mm\]\s*=\s*([^\r\n]+)",
        r"^;\s*filament_usage\s*[:=]\s*([^\r\n]+)",
    ])
    if filament is not None:
        md.filament_usage = _clamp_int(filament, 0, 0xFFFFFFFF)

    layer = _first_number(text, [
        r"^;\s*layer_height\s*=\s*([^\r\n]+)",
        r"^;\s*layer_height\s*:\s*([^\r\n]+)",
        r"^;\s*ORCA_LAYER_HEIGHT\s*:\s*([^\r\n]+)",
    ])
    if layer is not None:
        md.layer_height_microns = _clamp_int(layer * 1000.0, 0, 32767)

    shells = _first_number(text, [
        r"^;\s*perimeters\s*=\s*([^\r\n]+)",
        r"^;\s*perimeter_shells\s*:\s*([^\r\n]+)",
        r"^;\s*wall_loops\s*=\s*([^\r\n]+)",
    ])
    if shells is not None:
        md.shells = _clamp_int(shells, 0, 32767)

    speed = _first_number(text, [
        r"^;\s*perimeter_speed\s*=\s*([^\r\n]+)",
        r"^;\s*outer_wall_speed\s*=\s*([^\r\n]+)",
        r"^;\s*base_print_speed\s*:\s*([^\r\n]+)",
    ])
    if speed is not None:
        md.print_speed = _clamp_int(speed, 0, 32767)

    bed = _first_number(text, [
        r"^;\s*bed_temperature\s*=\s*([^\r\n]+)",
        r"^;\s*first_layer_bed_temperature\s*=\s*([^\r\n]+)",
        r"^;\s*platform_temperature\s*:\s*([^\r\n]+)",
    ])
    if bed is None:
        bed = _first_number(text, [r"^M(?:140|190)\s+[^\r\n;]*?S([-+]?\d+(?:\.\d+)?)"])
    if bed is not None:
        md.bed_temperature = _clamp_int(bed, -32768, 32767)

    temp = _first_number(text, [
        r"^;\s*temperature\s*=\s*([^\r\n]+)",
        r"^;\s*first_layer_temperature\s*=\s*([^\r\n]+)",
        r"^;\s*right_extruder_temperature\s*:\s*([^\r\n]+)",
    ])
    if temp is None:
        temp = _first_number(text, [r"^M(?:104|109)\s+[^\r\n;]*?S([-+]?\d+(?:\.\d+)?)"])
    if temp is not None:
        md.extruder_temperature = _clamp_int(temp, -32768, 32767)
        # FlashPrint 2.4.3 wrote the same value in both fields for this Finder.
        md.extruder_temperature_left = md.extruder_temperature

    return md


def strip_embedded_thumbnails(gcode: bytes) -> bytes:
    """Remove slicer thumbnail comment blocks; the GX container has its own BMP."""
    text = gcode.decode("utf-8-sig", "replace")
    out: list[str] = []
    skipping = False
    for line in text.splitlines(keepends=True):
        low = line.strip().lower()
        if re.match(r";\s*thumbnail(?:_[a-z0-9]+)?\s+begin\b", low):
            skipping = True
            continue
        if skipping and re.match(r";\s*thumbnail(?:_[a-z0-9]+)?\s+end\b", low):
            skipping = False
            continue
        if not skipping:
            out.append(line)
    result = "".join(out).encode("utf-8")
    return result if result.endswith((b"\n", b"\r")) else result + b"\n"


_FONT = {
    "O": ["01110", "10001", "10001", "10001", "10001", "10001", "01110"],
    "R": ["11110", "10001", "10001", "11110", "10100", "10010", "10001"],
    "C": ["01111", "10000", "10000", "10000", "10000", "10000", "01111"],
    "A": ["01110", "10001", "10001", "11111", "10001", "10001", "10001"],
}


def make_placeholder_bmp() -> bytes:
    pixels = [[0 for _ in range(WIDTH)] for _ in range(HEIGHT)]

    # Border and a simple ORCA label so this is visibly a generated placeholder.
    for x in range(4, WIDTH - 4):
        pixels[5][x] = 90
        pixels[HEIGHT - 6][x] = 90
    for y in range(5, HEIGHT - 5):
        pixels[y][4] = 90
        pixels[y][WIDTH - 5] = 90

    scale = 2
    gap = 3
    word = "ORCA"
    char_w = 5 * scale
    total_w = len(word) * char_w + (len(word) - 1) * gap
    start_x = (WIDTH - total_w) // 2
    start_y = (HEIGHT - 7 * scale) // 2
    for ci, ch in enumerate(word):
        pattern = _FONT[ch]
        ox = start_x + ci * (char_w + gap)
        for py, row in enumerate(pattern):
            for px, bit in enumerate(row):
                if bit == "1":
                    for dy in range(scale):
                        for dx in range(scale):
                            pixels[start_y + py * scale + dy][ox + px * scale + dx] = 220

    raw = bytearray()
    for y in range(HEIGHT - 1, -1, -1):
        for value in pixels[y]:
            raw.extend((value, value, value))

    image_size = len(raw)
    file_size = 54 + image_size
    bmp = bytearray()
    bmp += b"BM"
    bmp += struct.pack("<IHHI", file_size, 0, 0, 54)
    bmp += struct.pack("<IiiHHIIiiII", 40, WIDTH, HEIGHT, 1, 24, 0, image_size, 0x1274, 0x1274, 0, 0)
    bmp += raw
    if len(bmp) != BITMAP_SIZE:
        raise AssertionError(f"Generated BMP is {len(bmp)} bytes, expected {BITMAP_SIZE}")
    return bytes(bmp)


def build_gx(gcode: bytes, metadata: GXMetadata | None = None, strip_thumbnails: bool = True) -> bytes:
    if not gcode:
        raise GXError("G-code is empty")
    if gcode.startswith(GX_VERSION):
        validate_gx(gcode)
        return gcode

    # Refuse binary G-code or arbitrary binary files rather than uploading them as GX.
    sample = gcode[:65536]
    if b"\x00" in sample:
        raise GXError("Input contains NUL bytes and does not appear to be plain-text G-code")
    text = gcode.decode("utf-8-sig", "replace")
    if not re.search(r"(?m)^\s*[GMT]\d+\b", text):
        raise GXError("Input does not contain recognizable G-code commands")

    clean_gcode = strip_embedded_thumbnails(gcode) if strip_thumbnails else gcode
    md = metadata or parse_metadata(clean_gcode)
    header = bytearray()
    header += GX_VERSION
    header += struct.pack("<4I", 0, BITMAP_START, GCODE_START, GCODE_START)
    header += struct.pack(
        "<3I9h",
        md.print_time,
        md.filament_usage,
        md.filament_usage_left,
        md.multi_extruder_type,
        md.layer_height_microns,
        0,
        md.shells,
        md.print_speed,
        md.bed_temperature,
        md.extruder_temperature,
        md.extruder_temperature_left,
        md.final_field,
    )
    if len(header) != BITMAP_START:
        raise AssertionError(f"GX header is {len(header)} bytes, expected {BITMAP_START}")
    result = bytes(header) + make_placeholder_bmp() + clean_gcode
    validate_gx(result)
    return result


def validate_gx(data: bytes) -> dict[str, object]:
    if len(data) <= GCODE_START:
        raise GXError(f"GX file is too short: {len(data)} bytes")
    if data[:12] != GX_VERSION:
        raise GXError("Missing xgcode 1.0 header")
    constants = struct.unpack_from("<4I", data, 12)
    if constants != (0, BITMAP_START, GCODE_START, GCODE_START):
        raise GXError(f"Invalid GX offsets/constants: {constants}")
    fields = struct.unpack_from("<3I9h", data, 28)
    bmp = data[BITMAP_START:GCODE_START]
    if len(bmp) != BITMAP_SIZE or bmp[:2] != b"BM":
        raise GXError("GX preview is not the required 14,454-byte BMP")
    bmp_size = struct.unpack_from("<I", bmp, 2)[0]
    width, height, planes, bpp = struct.unpack_from("<iiHH", bmp, 18)
    if bmp_size != BITMAP_SIZE or (width, height, planes, bpp) != (WIDTH, HEIGHT, 1, 24):
        raise GXError(
            f"Invalid GX BMP: size={bmp_size}, dimensions={width}x{height}, planes={planes}, bpp={bpp}"
        )
    gcode = data[GCODE_START:]
    if not gcode:
        raise GXError("GX file contains no G-code")
    text = gcode[:262144].decode("utf-8", "replace")
    if not re.search(r"(?m)^\s*[GMT]\d+\b", text):
        raise GXError("GX payload does not contain recognizable G-code commands")
    return {
        "size": len(data),
        "gcode_size": len(gcode),
        "print_time": fields[0],
        "filament_usage": fields[1],
        "filament_usage_left": fields[2],
        "multi_extruder_type": fields[3],
        "layer_height_microns": fields[4],
        "shells": fields[6],
        "print_speed": fields[7],
        "bed_temperature": fields[8],
        "extruder_temperature": fields[9],
        "extruder_temperature_left": fields[10],
        "final_field": fields[11],
    }


def convert_file(source: Path, destination: Path | None = None) -> tuple[Path, dict[str, object], bool]:
    source = source.resolve()
    data = source.read_bytes()
    already_gx = data.startswith(GX_VERSION)
    result = build_gx(data)
    if destination is None:
        destination = source.with_suffix(".gx")
    destination = destination.resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(result)
    return destination, validate_gx(result), not already_gx


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Convert plain G-code into FlashForge Finder .gx")
    ap.add_argument("--version", action="version", version=f"finder_gx {VERSION}")
    sub = ap.add_subparsers(dest="action", required=True)
    c = sub.add_parser("convert")
    c.add_argument("source", type=Path)
    c.add_argument("destination", type=Path, nargs="?")
    i = sub.add_parser("inspect")
    i.add_argument("file", type=Path)
    args = ap.parse_args(argv)
    try:
        if args.action == "convert":
            output, info, converted = convert_file(args.source, args.destination)
            print(f"{'Converted' if converted else 'Validated'}: {output}")
            for key, value in info.items():
                print(f"{key}: {value}")
        else:
            info = validate_gx(args.file.read_bytes())
            print(f"Valid Finder GX: {args.file.resolve()}")
            for key, value in info.items():
                print(f"{key}: {value}")
        return 0
    except (OSError, GXError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
