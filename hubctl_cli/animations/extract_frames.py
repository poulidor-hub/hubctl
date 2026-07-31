#!/usr/bin/env python3
"""Extract frames from introHUB.tsx → hub_ascii.frames.gz as raw grids + color data."""
import gzip
import json
from pathlib import Path

TSX = Path(__file__).parent / "introHUB.tsx"
OUT = Path(__file__).parent / "hub_ascii.frames.gz"

DARK_PALETTE = {
    "c0": "#3a0a5c",
    "c1": "#6a1b9a",
    "c2": "#8e24aa",
    "c3": "#4a148c",
    "c4": "#ab47bc",
    "c5": "#ce93d8",
    "c6": "#e1bee7",
    "c7": "#f3e5f5",
}

TARGET_COLS = 98
TARGET_ROWS = 28


def extract_frame_data(lines: list[str]) -> str:
    for i, line in enumerate(lines):
        if "const FRAMES" in line:
            start = i
            break
    bracket_pos = lines[start].rfind("[")
    json_lines = [lines[start][bracket_pos:]]
    depth = 1
    for line in lines[start + 1:]:
        json_lines.append(line)
        for ch in line:
            if ch == "[":
                depth += 1
            elif ch == "]":
                depth -= 1
        if depth == 0:
            break
    json_str = "\n".join(json_lines).rstrip()
    if json_str.endswith(";"):
        json_str = json_str[:-1].rstrip()
    return json_str


def build_frame_data(frame: dict) -> dict:
    content = frame["content"]
    fg = frame.get("fgColors", {})
    bg = frame.get("bgColors", {})

    src_rows = len(content)
    src_cols = max(len(row) for row in content) if content else 0

    # Build raw char grid (crop to TARGET)
    chars = []
    for r in range(min(TARGET_ROWS, src_rows)):
        line = content[r] if r < len(content) else ""
        row_chars = []
        for c in range(min(TARGET_COLS, src_cols)):
            row_chars.append(line[c] if c < len(line) else " ")
        chars.append("".join(row_chars))
    # Pad rows
    while len(chars) < TARGET_ROWS:
        chars.append(" " * TARGET_COLS)

    # Build color runs: list of (row, start_col, end_col_exclusive, fg_hex, bg_hex)
    # Group consecutive cells with same colors into runs
    color_runs = []
    for r in range(min(TARGET_ROWS, src_rows)):
        run_start = 0
        run_fg = ""
        run_bg = ""
        for c in range(min(TARGET_COLS, src_cols)):
            key = f"{c},{r}"
            fg_hex = DARK_PALETTE.get(fg.get(key, ""), "")
            bg_hex = DARK_PALETTE.get(bg.get(key, ""), "")
            if (fg_hex, bg_hex) != (run_fg, run_bg):
                if run_start < c and (run_fg or run_bg):
                    color_runs.append([r, run_start, c, run_fg, run_bg])
                run_start = c
                run_fg = fg_hex
                run_bg = bg_hex
        # Final run
        if run_start < min(TARGET_COLS, src_cols) and (run_fg or run_bg):
            color_runs.append([r, run_start, min(TARGET_COLS, src_cols), run_fg, run_bg])

    return {
        "chars": chars,
        "colors": color_runs,
        "duration": round(frame.get("duration", 42)),
    }


def main():
    print(f"Reading {TSX}...")
    lines = TSX.read_text().splitlines()
    json_str = extract_frame_data(lines)
    print(f"Parsing frames...")
    frames_data = json.loads(json_str)
    print(f"Found {len(frames_data)} frames")

    all_chars = []
    all_colors = []
    all_durations = []
    for i, frame in enumerate(frames_data):
        d = build_frame_data(frame)
        all_chars.append(d["chars"])
        all_colors.append(d["colors"])
        all_durations.append(d["duration"])
        if (i + 1) % 20 == 0:
            print(f"  {i+1}/{len(frames_data)} frames processed...")

    data = {
        "chars": all_chars,
        "colors": all_colors,
        "durations": all_durations,
    }
    json_bytes = json.dumps(data, separators=(",", ":")).encode("utf-8")
    compressed = gzip.compress(json_bytes, compresslevel=9)

    print(f"Writing {OUT} ({len(compressed)} bytes compressed)...")
    OUT.write_bytes(compressed)
    print("Done.")


if __name__ == "__main__":
    main()
