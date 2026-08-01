"""Receipt photo preprocessing.

Phone photos are the worst-case input for a vision model: 12MP of mostly
tablecloth, often rotated only by an EXIF tag, sometimes HEIC. `normalize`
fixes all three, which is where essentially all of the accuracy and cost win
lives — a 4000px photo costs ~40x the base64 payload of a 1600px one and the
provider downscales it to roughly that anyway before the model ever sees it.

`deskew` is cosmetic by comparison. The vision model reads an angled receipt
fine; the flattened copy exists for the humans reading source_viewer and the
print packet months later during an audit.
"""

from __future__ import annotations

import math
from pathlib import Path

from loguru import logger
from PIL import Image, ImageOps

# HEIC is iPhone's default. Browser uploads usually transcode to JPEG on the
# way out, but files copied straight off the phone don't.
try:
    from pillow_heif import register_heif_opener

    register_heif_opener()
    HEIC_SUPPORTED = True
except ImportError:  # optional dep; everything else still works
    HEIC_SUPPORTED = False

SUFFIXES = {".jpg", ".jpeg", ".png", ".webp"} | ({".heic", ".heif"} if HEIC_SUPPORTED else set())

# Long edge in pixels. Vision providers tile images down to ~1.1k px regardless,
# so anything past this is paid-for detail the model never sees.
MAX_EDGE = 1600
JPEG_QUALITY = 82

# Flattened copies live beside the original as "<stem>.flat.jpg". They must never
# be treated as new raw files (they'd get their own .bean sidecar and reparse
# forever), so the ingest glob filters on this.
FLAT_MARKER = ".flat"


def is_flat_copy(path: Path) -> bool:
    return Path(path).stem.endswith(FLAT_MARKER)


def flat_path(path: Path) -> Path:
    return path.with_name(f"{path.stem}{FLAT_MARKER}.jpg")


def normalize(path: Path, max_edge: int = MAX_EDGE) -> Path:
    """Apply EXIF rotation, downscale, and re-encode as JPEG, in place.

    Returns the resulting path — which differs from the input when the source was
    a PNG/WEBP/HEIC, since the original is replaced by a .jpg. Idempotent: an
    already-normalized JPEG is left untouched rather than re-compressed.
    """
    with Image.open(path) as img:
        # exif_transpose bakes the Orientation tag into the pixels. Without it a
        # sideways photo stays sideways for any decoder that ignores the tag.
        img = ImageOps.exif_transpose(img)
        if img.mode not in ("RGB", "L"):
            img = img.convert("RGB")

        oversized = max(img.size) > max_edge
        if oversized:
            img.thumbnail((max_edge, max_edge), Image.LANCZOS)

        if not oversized and path.suffix.lower() in (".jpg", ".jpeg"):
            return path

        target = path.with_suffix(".jpg")
        img.save(target, "JPEG", quality=JPEG_QUALITY, optimize=True)

    if target != path:
        path.unlink()
    logger.debug("normalized {} -> {}", path.name, target.name)
    return target


# Above this fraction of the frame, the receipt already *is* the photo and a warp
# can only lose pixels off the edges. The prompt asks the model to omit corners in
# that case; it doesn't reliably do so, and its corner estimates drift by a percent
# or two between runs, which is enough to shave a header or a total off a
# full-frame document. Enforcing the rule here rather than trusting the model.
MAX_QUAD_AREA = 0.85

# There used to be a MIN_SKEW_DEGREES gate here, rejecting axis-aligned quads outright:
# corner detection was piggybacked on the same call that extracted the transaction, and
# a model optimized for reading totals correctly (not finding quads) reliably answered
# with a canned axis-aligned inset instead of a real quad — trimming to that box was
# pure clipping risk for zero benefit, so only a quad with visible skew was trusted.
# Corner detection is now its own call (see llm.CORNER_MODEL) to a model picked because
# it does the opposite — an axis-aligned answer from it is a real "the receipt sits in
# this box," worth cropping to even when there's no rotation to correct. _skew_degrees
# is still computed, for the debug log.


def _quad_area(corners: list[list[float]]) -> float:
    """Shoelace area of the quad, as a fraction of the (unit) image area."""
    total = 0.0
    for i in range(4):
        x1, y1 = corners[i]
        x2, y2 = corners[(i + 1) % 4]
        total += x1 * y2 - x2 * y1
    return abs(total) / 2


def _valid_quad(corners: list[list[float]]) -> bool:
    """Reject anything that isn't four sane, non-degenerate normalized points."""
    if not corners or len(corners) != 4:
        return False
    if any(len(pt) != 2 for pt in corners):
        return False
    if any(not (-0.05 <= v <= 1.05) for pt in corners for v in pt):
        return False
    xs = [p[0] for p in corners]
    ys = [p[1] for p in corners]
    # A quad covering under 15% of either axis is a misfire, not a receipt.
    if (max(xs) - min(xs)) <= 0.15 or (max(ys) - min(ys)) <= 0.15:
        return False
    return _quad_area(corners) <= MAX_QUAD_AREA


def _skew_degrees(corners: list[list[float]]) -> float:
    """Largest tilt of any quad edge away from the axis it should be parallel to."""
    tl, tr, br, bl = corners
    worst = 0.0
    for (ax, ay), (bx, by) in ((tl, tr), (bl, br)):  # should be horizontal
        worst = max(worst, abs(math.degrees(math.atan2(by - ay, bx - ax))))
    for (ax, ay), (bx, by) in ((tl, bl), (tr, br)):  # should be vertical
        worst = max(worst, abs(math.degrees(math.atan2(bx - ax, by - ay))))
    return worst


def deskew(path: Path, corners: list[list[float]], margin: float = 0.06) -> Path | None:
    """Write a perspective-corrected copy from four normalized corner points.

    `corners` is [[x, y], ...] in 0-1 image space, ordered top-left, top-right,
    bottom-right, bottom-left, as returned by the vision model.

    The quad is scaled outward about its centroid by `margin` before cropping.
    Model corner estimates routinely land a percent or two inside the real edge
    — in testing that was enough to shave the header off a receipt — and the
    error is not symmetric in cost: a band of tablecloth around the edge is
    harmless, a clipped total is a lost record. Hence the deliberately generous
    default.

    Returns the flattened copy's path, or None if the quad was unusable. Never
    raises: a failed cosmetic crop must not fail an ingest run.
    """
    if not _valid_quad(corners):
        logger.debug("skipping deskew of {}: unusable quad {}", path.name, corners)
        return None
    logger.debug(
        "deskewing {}: quad covers {:.0%} of frame, skewed {:.1f}°",
        path.name, _quad_area(corners), _skew_degrees(corners),
    )

    try:
        with Image.open(path) as img:
            w, h = img.size
            cx = sum(p[0] for p in corners) / 4
            cy = sum(p[1] for p in corners) / 4
            pts = [
                (
                    min(max((x + (x - cx) * margin) * w, 0), w),
                    min(max((y + (y - cy) * margin) * h, 0), h),
                )
                for x, y in corners
            ]
            tl, tr, br, bl = pts

            def dist(a, b):
                return ((a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2) ** 0.5

            out_w = max(int(max(dist(tl, tr), dist(bl, br))), 1)
            out_h = max(int(max(dist(tl, bl), dist(tr, br))), 1)

            # PIL's QUAD wants the source corners as NW, SW, SE, NE.
            quad = (*tl, *bl, *br, *tr)
            flat = img.transform((out_w, out_h), Image.QUAD, quad, Image.BICUBIC)
            target = flat_path(path)
            flat.convert("RGB").save(target, "JPEG", quality=JPEG_QUALITY, optimize=True)
    except Exception as exc:  # cosmetic step — degrade to "no flattened copy"
        logger.warning("deskew failed for {} ({}: {})", path.name, type(exc).__name__, exc)
        return None

    logger.debug("deskewed {} -> {} ({}x{})", path.name, target.name, out_w, out_h)
    return target
