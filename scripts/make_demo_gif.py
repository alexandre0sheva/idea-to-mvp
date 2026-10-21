"""Build the README's short GIF of the pipeline from the screenshots in docs/assets/.

    uv run python scripts/make_demo_gif.py

The frames are the stills the README already shows (the panel, a gate, the implementation board, the delivery
dashboard), in pipeline order; re-take the screenshots from a `DEMO_MODE=true` run and run this again to refresh it.
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image

ASSETS = Path(__file__).resolve().parents[1] / "docs" / "assets"
FRAMES = (
    "panel.png",
    "architecture-gate.png",
    "implementation-board.png",
    "delivery-dashboard.png",
)
OUTPUT = "demo-pipeline.gif"
WIDTH = 960
SECONDS_PER_FRAME = 2.2
BACKGROUND = (255, 255, 255)


def build_frames(folder: Path = ASSETS) -> list[Image.Image]:
    """The frames scaled to one width and padded (top aligned) to the tallest, as palette images."""
    scaled = []
    for name in FRAMES:
        with Image.open(folder / name) as image:
            rgb = image.convert("RGB")
            scaled.append(
                rgb.resize((WIDTH, round(rgb.height * WIDTH / rgb.width)), Image.Resampling.LANCZOS)
            )
    height = max(frame.height for frame in scaled)
    frames = []
    for frame in scaled:
        canvas = Image.new("RGB", (WIDTH, height), BACKGROUND)
        canvas.paste(frame, (0, 0))
        frames.append(
            canvas.quantize(colors=128, method=Image.Quantize.MEDIANCUT, dither=Image.Dither.NONE)
        )
    return frames


def main() -> int:
    frames = build_frames()
    target = ASSETS / OUTPUT
    frames[0].save(
        target,
        save_all=True,
        append_images=frames[1:],
        duration=int(SECONDS_PER_FRAME * 1000),
        loop=0,
        optimize=True,
    )
    print(f"Wrote {target} ({target.stat().st_size / 1024:.0f} KB, {len(frames)} frames)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
