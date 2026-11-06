"""Record a terminal session and render it, reproducibly, without a recorder installed.

    uv run --no-project --with pyte --with pillow python docker/demo-e2e/record.py \\
        --cast docs/assets/demo-e2e.cast --gif docs/assets/demo-e2e.gif \\
        --transcript docs/assets/demo-e2e.txt -- docker/demo-e2e.sh --screenshots docs/assets

The command runs in a pseudo-terminal of a fixed size, so it behaves as in a real terminal
(colours on). Its output is saved with timestamps as an asciicast v2 file (the format of
asciinema: play it with `asciinema play demo-e2e.cast`), as a plain-text transcript with the
escape sequences removed, and as a GIF: the screen is replayed with `pyte` (a terminal emulator),
drawn with Pillow, and encoded by ffmpeg with a small palette. Waits longer than --idle-max
seconds are shortened in the GIF so a stack start does not freeze it, and output appears line by
line so it can be read; the cast keeps real time.

`--render-only` redraws a GIF from an existing cast:  record.py --render-only --cast X --gif Y
"""

from __future__ import annotations

import argparse
import codecs
import fcntl
import json
import os
import pty
import re
import shutil
import signal
import struct
import subprocess
import sys
import tempfile
import termios
import time
from pathlib import Path

COLS, ROWS = 110, 34
FONT_SIZE = 13
FONTS = (
    "/System/Library/Fonts/Menlo.ttc",
    "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
    "/usr/share/fonts/dejavu/DejaVuSansMono.ttf",
)
ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07]*\x07")
THEME = {
    "default": "#d4d4d4",
    "black": "#3b3b3b",
    "red": "#f2777a",
    "green": "#99cc99",
    "brown": "#e8c46a",  # pyte names SGR 33 "brown"
    "yellow": "#e8c46a",
    "blue": "#6699cc",
    "magenta": "#cc99cc",
    "cyan": "#66cccc",
    "white": "#e6e6e6",
    "brightblack": "#8a8f98",
    "brightred": "#ff8f8f",
    "brightgreen": "#b5e8b5",
    "brightbrown": "#ffd98a",
    "brightyellow": "#ffd98a",
    "brightblue": "#8fb8e8",
    "brightmagenta": "#e8b8e8",
    "brightcyan": "#8fe8e8",
    "brightwhite": "#ffffff",
}
BACKGROUND = "#1b1d23"


def record(command: list[str], cols: int, rows: int) -> tuple[list[list], int, float]:
    """Run `command` in a pseudo-terminal; returns the output events, the exit code, the time."""
    master, slave = pty.openpty()
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
    env = {**os.environ, "TERM": "xterm-256color", "COLUMNS": str(cols), "LINES": str(rows)}
    process = subprocess.Popen(
        command, stdin=slave, stdout=slave, stderr=slave, env=env, start_new_session=True
    )
    os.close(slave)
    decoder = codecs.getincrementaldecoder("utf-8")("replace")
    events: list[list] = []
    started = time.monotonic()
    try:
        while True:
            try:
                data = os.read(master, 65536)
            except OSError:  # the child closed its side
                break
            if not data:
                break
            text = decoder.decode(data)
            if text:
                events.append([round(time.monotonic() - started, 4), "o", text])
    except KeyboardInterrupt:
        os.killpg(process.pid, signal.SIGTERM)  # the demo's own trap cleans up
    finally:
        os.close(master)
    return events, process.wait(), time.monotonic() - started


def write_cast(path: Path, events: list[list], cols: int, rows: int, command: list[str]) -> None:
    header = {
        "version": 2,
        "width": cols,
        "height": rows,
        "timestamp": int(time.time()),
        "title": " ".join(command),
        "env": {"TERM": "xterm-256color"},
    }
    lines = [json.dumps(header)] + [json.dumps(event, ensure_ascii=False) for event in events]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def read_cast(path: Path) -> tuple[dict, list[list]]:
    first, *rest = path.read_text(encoding="utf-8").splitlines()
    return json.loads(first), [json.loads(line) for line in rest if line.strip()]


def transcript(events: list[list]) -> str:
    """The text of the session: escape sequences dropped, carriage returns applied."""
    raw = "".join(e[2] for e in events if e[1] == "o")
    text = ANSI.sub("", raw).replace("\r\n", "\n")
    return "\n".join(line.split("\r")[-1].rstrip() for line in text.split("\n")).rstrip() + "\n"


def pause_after(line: str) -> float:
    """How long the GIF rests after a line so it can be read: longest after a headline."""
    plain = ANSI.sub("", line).strip()
    if re.match(r"==\s", plain):
        return 1.4
    return 0.6 if plain.startswith("$ ") else 0.0


def retime(events: list[list], idle_max: float, line_delay: float) -> list[tuple[float, str]]:
    """A timeline for reading: bursts of output appear line by line, waits are capped at
    `idle_max` seconds, and the GIF rests after headlines and commands. The cast keeps real time.
    """
    out, clock, previous = [], 0.0, 0.0
    for stamp, kind, data in events:
        if kind != "o":
            continue
        clock += min(stamp - previous, idle_max)
        previous = stamp
        for piece in data.splitlines(keepends=True):
            out.append((clock, piece))
            if piece.endswith("\n"):
                clock += line_delay + pause_after(piece)
    return out


def render_gif(
    cast: Path,
    gif: Path,
    *,
    idle_max: float,
    line_delay: float,
    fps: int,
    font_path: str | None,
    hold: float,
) -> int:
    import pyte
    from PIL import Image, ImageDraw, ImageFont

    header, events = read_cast(cast)
    cols, rows = header["width"], header["height"]
    path = font_path or next((f for f in FONTS if Path(f).is_file()), None)
    if path is None:
        raise SystemExit("no monospace font found; pass --font FILE")
    regular = ImageFont.truetype(path, FONT_SIZE, index=0)
    try:
        bold = ImageFont.truetype(path, FONT_SIZE, index=1)
    except OSError:
        bold = regular
    left, top, right, bottom = regular.getbbox("M")
    cell_w = round(regular.getlength("M"))
    cell_h = bottom - top + 5
    pad = 12
    size = (cols * cell_w + 2 * pad, rows * cell_h + 2 * pad)

    screen = pyte.Screen(cols, rows)
    stream = pyte.Stream(screen)
    timeline = retime(events, idle_max, line_delay)

    def colour(name: str, fallback: str) -> str:
        if name in THEME:
            return THEME[name]
        return f"#{name}" if re.fullmatch(r"[0-9a-fA-F]{6}", name) else fallback

    def draw() -> Image.Image:
        image = Image.new("RGB", size, BACKGROUND)
        pen = ImageDraw.Draw(image)
        pen.fontmode = "1"  # hard-edged glyphs: far fewer colours, a much smaller GIF
        for y in range(rows):
            line = screen.buffer[y]
            for x in range(cols):
                char = line[x]
                fg = colour(char.fg, THEME["default"])
                bg = colour(char.bg, BACKGROUND) if char.bg != "default" else BACKGROUND
                if char.reverse:
                    fg, bg = bg, fg
                px, py = pad + x * cell_w, pad + y * cell_h
                if bg != BACKGROUND:
                    pen.rectangle([px, py, px + cell_w, py + cell_h], fill=bg)
                if char.data.strip():
                    pen.text(
                        (px, py - top), char.data, fill=fg, font=bold if char.bold else regular
                    )
        return image

    def key() -> tuple:
        return tuple(
            tuple((c.data, c.fg, c.bg, c.bold, c.reverse) for c in screen.buffer[y].values())
            for y in range(rows)
        )

    tmp = Path(tempfile.mkdtemp(prefix="record-frames-"))
    try:
        frames: list[tuple[Path, float]] = []
        last_key = None
        step = 1 / fps
        total = timeline[-1][0] if timeline else 0.0
        index, count = 0, 0
        for n in range(int(total / step) + 2):
            moment = n * step
            while index < len(timeline) and timeline[index][0] <= moment:
                stream.feed(timeline[index][1])
                index += 1
            current = key()
            if current == last_key and frames:
                frames[-1] = (frames[-1][0], frames[-1][1] + step)
                continue
            last_key = current
            frame = tmp / f"f{count:05d}.png"
            draw().save(frame)
            frames.append((frame, step))
            count += 1
        frames[-1] = (frames[-1][0], frames[-1][1] + hold)
        listing = tmp / "frames.txt"
        listing.write_text(
            "".join(f"file '{f.name}'\nduration {d:.3f}\n" for f, d in frames)
            + f"file '{frames[-1][0].name}'\n",
            encoding="utf-8",
        )
        filters = (
            "split[a][b];[a]palettegen=max_colors=12:stats_mode=full[p];"
            "[b][p]paletteuse=dither=none:diff_mode=rectangle"
        )
        subprocess.run(
            ["ffmpeg", "-v", "error", "-y", "-f", "concat", "-safe", "0", "-i", str(listing),
             "-vf", filters, "-fps_mode", "vfr", "-loop", "0", str(gif.resolve())],
            cwd=tmp, check=True,
        )  # fmt: skip
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print(f"{gif}: {len(frames)} frames, {gif.stat().st_size / 1e6:.2f} MB")
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    command: list[str] = []
    if "--" in argv:  # everything after `--` is the command to record
        split = argv.index("--")
        argv, command = argv[:split], argv[split + 1 :]
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--render-only", action="store_true", help="redraw the GIF from --cast")
    parser.add_argument("--cast", type=Path)
    parser.add_argument("--gif", type=Path)
    parser.add_argument("--transcript", type=Path)
    parser.add_argument("--cols", type=int, default=COLS)
    parser.add_argument("--rows", type=int, default=ROWS)
    parser.add_argument("--idle-max", type=float, default=0.8)
    parser.add_argument("--line-delay", type=float, default=0.06, help="seconds per output line")
    parser.add_argument("--fps", type=int, default=6)
    parser.add_argument("--hold", type=float, default=4.0, help="seconds the last frame stays")
    parser.add_argument("--font")
    args = parser.parse_args(argv)
    if args.cast is None:
        parser.error("--cast is required")
    if args.render_only:
        if args.gif is None:
            parser.error("--render-only needs --gif")
        return render_gif(
            args.cast,
            args.gif,
            idle_max=args.idle_max,
            line_delay=args.line_delay,
            fps=args.fps,
            font_path=args.font,
            hold=args.hold,
        )
    if not command:
        parser.error("give the command to record after `--`")
    events, code, took = record(command, args.cols, args.rows)
    args.cast.parent.mkdir(parents=True, exist_ok=True)
    write_cast(args.cast, events, args.cols, args.rows, command)
    print(f"recorded {len(events)} events in {took:.0f}s (exit {code}) -> {args.cast}")
    if args.transcript:
        args.transcript.write_text(transcript(events), encoding="utf-8")
        print(f"transcript -> {args.transcript}")
    if code != 0:
        return code
    if args.gif:
        return render_gif(
            args.cast,
            args.gif,
            idle_max=args.idle_max,
            line_delay=args.line_delay,
            fps=args.fps,
            font_path=args.font,
            hold=args.hold,
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
