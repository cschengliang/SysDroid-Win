"""Regenerate the SysDroid application icon (droid head with a glowing '>_' screen).

macOS 26 "Liquid Glass" style, Clear Glass treatment, on the brand-blue (#2878b5) tile.
Hand-written SVG on a 1024 canvas with four layer groups (back -> front):
  1 background tile, 2 droid head glass, 3 screen glass, 4 '>_' glow.
Masters:
  sysdroid.svg       Apple macOS grid (824 px squircle, 100 px margin): 64-256 px.
  sysdroid_tight.svg same art with ~2.5% margin so it fills a Windows taskbar slot: 32/48 px.
  sysdroid_s16.svg / sysdroid_s24.svg  simplified masters for 16/24 px.
Outputs (relative to assets/): icons/sysdroid-<size>.png for the in-app QIcon and
SysDroid.ico (16-128 px as 32-bit BMP frames, 256 px PNG-compressed) for the EXE.

Not part of the app or the build. Run with any Python that has resvg_py and Pillow:
    python -m pip install resvg_py pillow
    python assets/icon-src/make_icon.py
"""
import io
import math
import struct
from pathlib import Path

SRC = Path(__file__).resolve().parent
ASSETS = SRC.parent
SIZES = (16, 24, 32, 48, 64, 128, 256)

VARIANT = dict(title="Clear Glass droid", clear=True)
GLOW, GLOW_CORE = "#4dff8a", "#e4ffee"


def squircle(x0: float, size: float, n: float = 4.4, steps: int = 240) -> str:
    """Superellipse |x|^n + |y|^n = 1, a close stand-in for Apple's continuous-curvature shape."""
    c, r = x0 + size / 2, size / 2
    pts = []
    for i in range(steps):
        t = 2 * math.pi * i / steps
        ct, st = math.cos(t), math.sin(t)
        x = c + r * math.copysign(abs(ct) ** (2 / n), ct)
        y = c + r * math.copysign(abs(st) ** (2 / n), st)
        pts.append(f"{x:.1f},{y:.1f}")
    return "M" + " L".join(pts) + " Z"


def art(v: dict, grid: str) -> str:
    small = grid in ("s16", "s24")
    x0, size = (100, 824) if grid == "mac" else (26, 972)
    tile = squircle(x0, size)
    # Scale the foreground so it keeps the same proportion inside the tile.
    k = size / 824
    fg = f'translate({512 - 512 * k:.1f} {512 - 512 * k:.1f}) scale({k:.4f})'
    clear = v["clear"] and not small

    head = "M212,690 A300,300 0 0 1 812,690 L812,700 Q812,744 768,744 L256,744 Q212,744 212,700 Z"
    screen = dict(x=300, y=520, w=424, h=160, rx=72)
    prompt = '<polyline points="372,558 436,600 372,642"/><line x1="470" y1="642" x2="566" y2="642"/>'
    rods = '<line x1="404" y1="440" x2="338" y2="282"/><line x1="620" y1="440" x2="686" y2="282"/>'
    orbs = [(332, 268), (692, 268)]
    pw, rod_w = 30, 34
    if small:
        # Bigger silhouette: dome fills the tile, screen larger, short thick antennas.
        head = "M150,770 A362,362 0 0 1 874,770 L874,790 Q874,840 824,840 L200,840 Q150,840 150,790 Z"
        screen = dict(x=262, y=560, w=500, h=220, rx=96)
        rods = '<line x1="392" y1="460" x2="318" y2="250"/><line x1="632" y1="460" x2="706" y2="250"/>'
        orbs = []
        rod_w = 86
        if grid == "s16":
            prompt, pw = '<polyline points="430,600 530,670 430,740"/>', 92
        else:
            prompt, pw = '<polyline points="350,604 430,670 350,736"/><line x1="490" y1="736" x2="640" y2="736"/>', 66
    sx, sy, sw, sh, srx = screen["x"], screen["y"], screen["w"], screen["h"], screen["rx"]

    head_fill = "url(#headClear)" if clear else "url(#headFrost)"
    orb_svg = "".join(
        f'<circle cx="{x}" cy="{y}" r="40" fill="url(#orb)"/>'
        f'<ellipse cx="{x - 10}" cy="{y - 16}" rx="17" ry="10" fill="#ffffff" fill-opacity="0.85"/>'
        f'<circle cx="{x}" cy="{y}" r="38.5" fill="none" stroke="#ffffff" stroke-opacity="0.55" stroke-width="3"/>'
        for x, y in orbs)
    refraction = (f'<g clip-path="url(#headClip)"><rect x="0" y="0" width="1024" height="1024" fill="url(#bgRefract)" '
                  f'filter="url(#frost)"/></g>') if clear else ""
    blur = 0.5 if small else 1.0

    return f"""<svg xmlns="http://www.w3.org/2000/svg" width="1024" height="1024" viewBox="0 0 1024 1024">
  <!-- SysDroid app icon ({v['title']}) · grid: {grid}. Light from top; layers: 1 background, 2 head, 3 screen, 4 prompt. -->
  <defs>
    <clipPath id="tileClip"><path d="{tile}"/></clipPath>
    <clipPath id="headClip"><path d="{head}" transform="{fg}"/></clipPath>
    <linearGradient id="bg" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0" stop-color="#5aaeea"/><stop offset="0.55" stop-color="#2878b5"/><stop offset="1" stop-color="#17507f"/>
    </linearGradient>
    <radialGradient id="bgBloom" cx="0.5" cy="0.62" r="0.5">
      <stop offset="0" stop-color="#8fd0ff" stop-opacity="0.45"/><stop offset="1" stop-color="#8fd0ff" stop-opacity="0"/>
    </radialGradient>
    <linearGradient id="bgRefract" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0" stop-color="#bfe3ff"/><stop offset="0.6" stop-color="#5da9e2"/><stop offset="1" stop-color="#2a7cba"/>
    </linearGradient>
    <linearGradient id="rim" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0" stop-color="#ffffff" stop-opacity="0.9"/><stop offset="0.25" stop-color="#ffffff" stop-opacity="0.25"/>
      <stop offset="0.7" stop-color="#ffffff" stop-opacity="0.05"/><stop offset="1" stop-color="#ffffff" stop-opacity="0.35"/>
    </linearGradient>
    <linearGradient id="headFrost" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0" stop-color="#ffffff"/><stop offset="0.55" stop-color="#f1f6fb" stop-opacity="0.97"/>
      <stop offset="1" stop-color="#cfe0ef" stop-opacity="0.92"/>
    </linearGradient>
    <linearGradient id="headClear" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0" stop-color="#ffffff" stop-opacity="0.55"/><stop offset="0.6" stop-color="#e8f4ff" stop-opacity="0.28"/>
      <stop offset="1" stop-color="#d4ecff" stop-opacity="0.38"/>
    </linearGradient>
    <linearGradient id="edgeLight" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0" stop-color="#ffffff" stop-opacity="1"/><stop offset="0.35" stop-color="#ffffff" stop-opacity="0.35"/>
      <stop offset="0.8" stop-color="#ffffff" stop-opacity="0.1"/><stop offset="1" stop-color="#ffffff" stop-opacity="0.7"/>
    </linearGradient>
    <linearGradient id="rod" x1="0" y1="0" x2="1" y2="0">
      <stop offset="0" stop-color="#d6e4f0"/><stop offset="0.45" stop-color="#ffffff"/><stop offset="1" stop-color="#b7c9d9"/>
    </linearGradient>
    <radialGradient id="orb" cx="0.4" cy="0.3" r="0.8">
      <stop offset="0" stop-color="#c9ffdc"/><stop offset="0.5" stop-color="#3ddc84"/><stop offset="1" stop-color="#169a52"/>
    </radialGradient>
    <linearGradient id="screen" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0" stop-color="#123a33"/><stop offset="1" stop-color="#04120f"/>
    </linearGradient>
    <radialGradient id="screenBloom" cx="0.4" cy="0.6" r="0.6">
      <stop offset="0" stop-color="{GLOW}" stop-opacity="0.3"/><stop offset="1" stop-color="{GLOW}" stop-opacity="0"/>
    </radialGradient>
    <linearGradient id="screenGloss" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0" stop-color="#ffffff" stop-opacity="0.22"/><stop offset="0.45" stop-color="#ffffff" stop-opacity="0.04"/>
      <stop offset="0.46" stop-color="#ffffff" stop-opacity="0"/>
    </linearGradient>
    <filter id="tileShadow" x="-10%" y="-10%" width="120%" height="125%">
      <feDropShadow dx="0" dy="{8 if grid == 'mac' else 4}" stdDeviation="{12 if grid == 'mac' else 5}" flood-color="#0b2236" flood-opacity="0.28"/>
    </filter>
    <filter id="headShadow" x="-20%" y="-20%" width="140%" height="150%">
      <feDropShadow dx="0" dy="{18 * blur:.0f}" stdDeviation="{22 * blur:.0f}" flood-color="#0a3358" flood-opacity="0.35"/>
    </filter>
    <filter id="softEdge" x="-20%" y="-20%" width="140%" height="140%"><feGaussianBlur stdDeviation="{5 if small else 7}"/></filter>
    <filter id="frost" x="-5%" y="-5%" width="110%" height="110%"><feGaussianBlur stdDeviation="18"/></filter>
    <filter id="inset" x="-10%" y="-10%" width="120%" height="130%">
      <feOffset dy="{10 * blur:.0f}" in="SourceAlpha" result="o"/><feGaussianBlur stdDeviation="{12 * blur:.0f}" in="o" result="b"/>
      <feComposite in="SourceAlpha" in2="b" operator="out" result="inv"/>
      <feFlood flood-color="#000000" flood-opacity="0.7"/><feComposite operator="in" in2="inv" result="sh"/>
      <feMerge><feMergeNode in="SourceGraphic"/><feMergeNode in="sh"/></feMerge>
    </filter>
    <filter id="bottomShade" x="-10%" y="-10%" width="120%" height="120%">
      <feOffset dy="-{16 * blur:.0f}" in="SourceAlpha" result="o"/><feGaussianBlur stdDeviation="{20 * blur:.0f}" in="o" result="b"/>
      <feComposite in="SourceAlpha" in2="b" operator="out" result="inv"/>
      <feFlood flood-color="#1f4f7a" flood-opacity="{0.22 if not clear else 0.12}"/><feComposite operator="in" in2="inv" result="sh"/>
      <feMerge><feMergeNode in="SourceGraphic"/><feMergeNode in="sh"/></feMerge>
    </filter>
    <filter id="glow" x="-30%" y="-60%" width="160%" height="220%">
      <feGaussianBlur stdDeviation="{16 if small else 14}" result="b"/>
      <feMerge><feMergeNode in="b"/><feMergeNode in="b"/><feMergeNode in="SourceGraphic"/></feMerge>
    </filter>
  </defs>

  <g id="1-background">
    <path d="{tile}" fill="url(#bg)" filter="url(#tileShadow)"/>
    <g clip-path="url(#tileClip)">
      <rect width="1024" height="1024" fill="url(#bgBloom)"/>
    </g>
    <path d="{tile}" fill="none" stroke="url(#rim)" stroke-width="{10 if small else 6}" transform="translate(512 512) scale({1 - 6 / size:.4f}) translate(-512 -512)"/>
  </g>

  <g id="2-head" transform="{fg}">
    <g stroke="url(#rod)" stroke-linecap="round" stroke-width="{rod_w}">{rods}</g>
    {orb_svg}
    <g filter="url(#headShadow)"><path d="{head}" fill="{head_fill}" filter="url(#bottomShade)"/></g>
  </g>
  {refraction.replace('url(#headClip)', 'url(#headClip)')}
  <g transform="{fg}">
    {'<path d="' + head + '" fill="url(#headClear)"/>' if clear else ''}
    <path d="{head}" fill="none" stroke="url(#edgeLight)" stroke-width="{12 if small else 7}"/>
    {'' if small else '<path d="M330,486 Q404,420 512,410" fill="none" stroke="#ffffff" stroke-opacity="0.95" stroke-width="18" stroke-linecap="round" filter="url(#softEdge)"/>'}
  </g>

  <g clip-path="url(#headClip)">
    <path d="{head}" transform="{fg}" fill="none" stroke="#ffffff" stroke-opacity="{0.75 if clear else 0.55}" stroke-width="{70 if small else 46}" filter="url(#softEdge)"/>
  </g>

  <g id="3-screen" transform="{fg}">
    <rect x="{sx}" y="{sy}" width="{sw}" height="{sh}" rx="{srx}" fill="url(#screen)" filter="url(#inset)"/>
    <rect x="{sx}" y="{sy}" width="{sw}" height="{sh}" rx="{srx}" fill="url(#screenBloom)"/>
  </g>

  <g id="4-prompt" transform="{fg}">
    <g fill="none" stroke="{GLOW}" stroke-linecap="round" stroke-linejoin="round" stroke-width="{pw}" filter="url(#glow)">{prompt}</g>
    <g fill="none" stroke="{GLOW_CORE}" stroke-linecap="round" stroke-linejoin="round" stroke-width="{pw * 0.36:.0f}">{prompt}</g>
    <rect x="{sx}" y="{sy}" width="{sw}" height="{sh}" rx="{srx}" fill="url(#screenGloss)"/>
    <rect x="{sx + 3}" y="{sy + 3}" width="{sw - 6}" height="{sh - 6}" rx="{srx - 3}" fill="none" stroke="url(#edgeLight)" stroke-opacity="0.6" stroke-width="{8 if small else 4}"/>
  </g>
</svg>
"""


def render(svg: str, px: int, supersample: int = 1):
    import resvg_py
    from PIL import Image
    big = Image.open(io.BytesIO(resvg_py.svg_to_bytes(svg_string=svg, width=px * supersample, height=px * supersample)))
    return big if supersample == 1 else big.resize((px, px), Image.LANCZOS)


def write_ico(path: Path, frames: dict) -> None:
    """ICO with 32-bit BGRA DIB frames below 256 px and a PNG frame at 256 px."""
    entries, blobs = [], []
    for size in sorted(frames):
        image = frames[size].convert("RGBA")
        if size >= 256:
            buffer = io.BytesIO(); image.save(buffer, "PNG"); blob = buffer.getvalue()
        else:
            header = struct.pack("<IiiHHIIiiII", 40, size, size * 2, 1, 32, 0, 0, 0, 0, 0, 0)
            pixels = image.tobytes("raw", "BGRA", 0, -1)  # bottom-up rows
            row = ((size + 31) // 32) * 4
            mask = bytes(row * size)  # alpha channel carries transparency; AND mask all zero
            blob = header + pixels + mask
        entries.append((size, blob))
    offset = 6 + 16 * len(entries)
    out = [struct.pack("<HHH", 0, 1, len(entries))]
    for size, blob in entries:
        out.append(struct.pack("<BBBBHHII", size % 256, size % 256, 0, 0, 1, 32, len(blob), offset))
        offset += len(blob)
    out += [blob for _, blob in entries]
    path.write_bytes(b"".join(out))


if __name__ == "__main__":
    masters = {grid: art(VARIANT, grid) for grid in ("mac", "tight", "s16", "s24")}
    for grid, svg in masters.items():
        (SRC / f"sysdroid{'' if grid == 'mac' else '_' + grid}.svg").write_text(svg, encoding="utf-8", newline="\n")
    plan = {16: ("s16", 4), 24: ("s24", 4), 32: ("tight", 2), 48: ("tight", 2),
            64: ("mac", 1), 128: ("mac", 1), 256: ("mac", 1)}
    frames = {}
    (ASSETS / "icons").mkdir(exist_ok=True)
    for size in SIZES:
        grid, supersample = plan[size]
        frames[size] = render(masters[grid], size, supersample)
        frames[size].save(ASSETS / "icons" / f"sysdroid-{size}.png", optimize=True)
    write_ico(ASSETS / "SysDroid.ico", frames)
    print("wrote", ASSETS / "SysDroid.ico", "and", len(frames), "PNGs")
