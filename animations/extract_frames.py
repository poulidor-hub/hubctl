#!/usr/bin/env python3
"""Extract frames from introHUB.tsx → hub_ascii.frames.gz with Rich color support."""
import gzip
import json
import sys
from pathlib import Path

TSX = Path(__file__).parent / "introHUB.tsx"
OUT = Path(__file__).parent / "hub_ascii.frames.gz"

# Rich color names mapped from TSX hex palette
HEX_TO_RICH = {
    "#10002b": "#10002b",
    "#3c096c": "#3c096c",
    "#5a189a": "#5a189a",
    "#240046": "#240046",
    "#7b2cbf": "#7b2cbf",
    "#9d4edd": "#9d4edd",
    "#c77dff": "#c77dff",
    "#e0aaff": "#e0aaff",
    "#3c264d": "#3c264d",
    "#43334d": "#43334d",
}

DARK_PALETTE = {
    "c0": "#10002b",
    "c1": "#3c096c",
    "c2": "#5a189a",
    "c3": "#240046",
    "c4": "#7b2cbf",
    "c5": "#9d4edd",
    "c6": "#c77dff",
    "c7": "#e0aaff",
}

TARGET_COLS = 98
TARGET_ROWS = 10


def extract_frame_data(lines: list[str]) -> str:
    """Extract the JSON array from the TSX source."""
    start = None
    for i, line in enumerate(lines):
        if "const FRAMES" in line:
            start = i
            break
    if start is None:
        raise ValueError("FRAMES not found")
    # Find the opening bracket position
    bracket_pos = lines[start].rfind("[")
    # Extract from [ onward, replacing the prefix line
    json_lines = [lines[start][bracket_pos:]]
    brace_depth = 1  # the opening [ is on the first line
    for line in lines[start + 1:]:
        json_lines.append(line)
        for ch in line:
            if ch == "[":
                brace_depth += 1
            elif ch == "]":
                brace_depth -= 1
        if brace_depth == 0 and "]" in line:
            break
    json_str = "\n".join(json_lines)
    # Strip trailing TypeScript semicolon
    json_str = json_str.rstrip()
    if json_str.endswith(";"):
        json_str = json_str[:-1].rstrip()
    return json_str


def render_frame_to_rich(frame: dict) -> str:
    """Convert a TSX frame to a Rich-renderable string with per-cell colors."""
    content = frame["content"]
    fg = frame.get("fgColors", {})
    bg = frame.get("bgColors", {})

    src_rows = len(content)
    src_cols = max(len(row) for row in content) if content else 0

    # Build a 2D grid of (char, fg_hex, bg_hex)
    grid = []
    for r in range(src_rows):
        row = []
        line = content[r] if r < len(content) else ""
        for c in range(src_cols):
            ch = line[c] if c < len(line) else " "
            key = f"{c},{r}"
            fg_hex = DARK_PALETTE.get(fg.get(key, ""), "")
            bg_hex = DARK_PALETTE.get(bg.get(key, ""), "")
            row.append((ch, fg_hex, bg_hex))
        grid.append(row)

    # Crop+scale to TARGET_COLS × TARGET_ROWS
    rows_out = []
    for r in range(min(TARGET_ROWS, src_rows)):
        parts = []
        for c in range(min(TARGET_COLS, src_cols)):
            ch, fg_hex, bg_hex = grid[r][c]
            styles = []
            if fg_hex:
                styles.append(fg_hex)
            if bg_hex:
                styles.append(f"on {bg_hex}")
            if styles:
                parts.append(f"[{','.join(styles)}]{ch}[/]")
            else:
                parts.append(ch)
        rows_out.append("".join(parts))

    # Pad rows to TARGET_ROWS
    while len(rows_out) < TARGET_ROWS:
        rows_out.append(" " * TARGET_COLS)

    return "\n".join(rows_out)


def main():
    print(f"Reading {TSX}...")
    lines = TSX.read_text().splitlines()
    json_str = extract_frame_data(lines)
    print(f"Parsing {len(json_str)} bytes of JSON...")
    frames_data = json.loads(json_str)
    print(f"Found {len(frames_data)} frames")

    output_frames = []
    output_durations = []
    for i, frame in enumerate(frames_data):
        rich_frame = render_frame_to_rich(frame)
        output_frames.append(rich_frame)
        dur_ms = round(frame.get("duration", 42))
        output_durations.append(dur_ms)
        if (i + 1) % 10 == 0:
            print(f"  {i+1}/{len(frames_data)} frames rendered...")

    data = {"frames": output_frames, "durations": output_durations}
    json_bytes = json.dumps(data, separators=(",", ":")).encode("utf-8")
    compressed = gzip.compress(json_bytes, compresslevel=9)

    print(f"Writing {OUT} ({len(compressed)} bytes compressed)...")
    OUT.write_bytes(compressed)
    print("Done.")


if __name__ == "__main__":
    main()
