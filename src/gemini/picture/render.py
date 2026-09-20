"""Pixels to braille characters: a picture that fits into one chat message.

Why braille and not plain ASCII. Twitch chat is drawn in a proportional
font, so lines made of `.:-=+*#%@` drift apart, and runs of spaces collapse –
half of the art simply disappears. Braille characters (U+2800–U+28FF)
render at one width, the blank character U+2800 is not a space and does
not collapse, and the density is four times higher: one cell carries a
2x4 dot matrix.

Why everything goes out as one message. A braille line contains no spaces,
so for the browser it is one unbreakable "word". Spaces sit only at the joins
between lines, and wrapping happens strictly there – so every line lands on its
own visual line by itself, without newlines, which a Twitch message does not
have anyway. The sender's nick does not get in the way: the first line of art
does not fit into the rest of the first visual line and moves to the second.

Hence the size: the budget is in message characters, not in lines.
"""
import io
import logging

from PIL import Image, ImageChops, ImageFilter, ImageOps

from src.core.config import Picture

logger = logging.getLogger(__name__)

# A braille character is 0x2800 plus an eight-dot mask. The dots in a 2x4 cell are
# numbered not in reading order but historically: 1,2,3,7 on the left, 4,5,6,8 on the right
DOT_BITS = {
    (0, 0): 0x01, (0, 1): 0x02, (0, 2): 0x04, (0, 3): 0x40,
    (1, 0): 0x08, (1, 1): 0x10, (1, 2): 0x20, (1, 3): 0x80,
}
BLANK = '⠀'
DOTS_X = 2
DOTS_Y = 4

# Bounds for choosing the frame: narrower – nothing can be made out, lower – not a picture
MIN_COLS = 14
MIN_ROWS = 6

# Brightness threshold below which a dot counts as filled
THRESHOLD = 128

# Share of filled dots above which the picture counts as flooded. A single
# threshold for the whole picture fails when a large area lies just slightly
# darker than it: a green frog turns entirely into ink and leaves a silhouette
# with no face. Then we switch to a local threshold
INK_MAX = 0.45
# And the other way round: a nearly empty result also means the threshold missed
INK_MIN = 0.05

# Local threshold: a pixel darker than its surroundings by BIAS is ink. The radius
# is in dots of the finished picture, hence small
LOCAL_RADIUS = 2.0
LOCAL_BIAS = 6

# A pixel counts as part of the silhouette if it is at least half opaque
ALPHA_SOLID = 128

# Size of the copy sent to Gemini: it only has to understand what is in the
# picture and decide whether it may be shown – resolution does not affect that
PREVIEW_PX = 512
PREVIEW_QUALITY = 85

# Cap on the decompressed size: a compressed picture of a couple of megabytes
# expands into hundreds of megapixels, and decoding would eat the memory
MAX_PIXELS = 40_000_000


def render(data: bytes, *, limit: int, max_cols: int) -> str | None:
    """A finished chat message, or None if drawing failed.

    A blocking function: decoding and resizing are CPU-bound,
    so it should be called via asyncio.to_thread.
    """
    try:
        img = Image.open(io.BytesIO(data))
        # The size is already known from the header, and must be checked BEFORE load():
        # a hundred-kilobyte png expands into hundreds of megapixels, and
        # decoding would eat the memory before we get a chance to refuse
        if img.width * img.height > MAX_PIXELS:
            logger.info('!ascii: картинка слишком большая: %dx%d', img.width, img.height)
            return None
        img.load()
    except Exception:
        logger.info('!ascii: файл не открылся как картинка')
        return None

    img, shape = _to_grayscale(img)
    # A chat cell is taller than twice its width, so a dot is not square:
    # uncorrected, the picture looks stretched vertically. Squeeze it up front,
    # computing the geometry from the "on-screen" height
    height = max(1, round(img.height * Picture.ASPECT))
    cols, rows = _best_box(img.width, height, limit, max_cols)
    size = _fit(img.width, height, cols, rows)
    img = img.resize(size, Image.LANCZOS)

    ink = _binarize(img)
    if shape is not None:
        ink = _add_shape(ink, shape.resize(size, Image.LANCZOS))

    lines = _trim(_to_lines(ink))
    if not lines:
        return None
    return ' '.join(lines)


def preview(data: bytes, max_side: int = PREVIEW_PX) -> tuple[bytes, str] | None:
    """A downscaled copy for Gemini. Blocking, call via to_thread.

    The model has to say what is drawn and whether it may be shown – a small
    picture is enough for that. There is no reason to send the original: a phone
    photo weighs megabytes and costs accordingly, while the decision does not
    change with resolution.
    """
    try:
        img = Image.open(io.BytesIO(data))
        if img.width * img.height > MAX_PIXELS:
            return None
        img.load()
        # JPEG has no transparency, and it is not needed here anyway
        if img.mode not in ('RGB', 'L'):
            img = img.convert('RGB')
        img.thumbnail((max_side, max_side), Image.LANCZOS)
        buffer = io.BytesIO()
        img.save(buffer, format='JPEG', quality=PREVIEW_QUALITY)
    except Exception:
        logger.info('!ascii: не удалось уменьшить картинку для Gemini', exc_info=True)
        return None
    return buffer.getvalue(), 'image/jpeg'


def _binarize(img: Image.Image) -> Image.Image:
    """Halftones to black and white, already at the final size.

    First we try a plain threshold: on a high-contrast drawing it gives the
    cleanest lines. If the picture floods from it or, conversely, nearly
    disappears, we take a local threshold – it compares a dot not with one global
    number but with its surroundings, and so pulls out outlines and features inside
    large flat areas. Dithering is no good here at all: at sixty-odd dots across,
    Floyd-Steinberg gives noise, not halftones.
    """
    ink = img.point(lambda v: 255 if v > THRESHOLD else 0, mode='1')
    share = _ink_share(ink)
    if not INK_MIN <= share <= INK_MAX:
        logger.debug('!ascii: порог залил %.0f%%, беру локальный', share * 100)
        ink = ImageChops.subtract(
            img.filter(ImageFilter.GaussianBlur(LOCAL_RADIUS)), img,
        ).point(lambda v: 0 if v > LOCAL_BIAS else 255, mode='1')
    if not Picture.HOLLOW:
        return ink
    return _hollow(ink, img)


def _hollow(ink: Image.Image, gray: Image.Image) -> Image.Image:
    """Hollow out the fill, keeping the edges and the deepest shadows.

    A solid black blob loses its shape: on a raccoon photo the contour is visible
    but the body is a smudge. We subtract from the drawing itself eroded by one dot:
    only the edges remain. Thin lines stay intact – they consist of edge
    themselves, so hollowing does nothing to a drawn Pepe.

    So that not just a wire diagram is left, the darkest places (darker than
    `Picture.SHADOW`) are put back on top – they give the volume: the shadow is
    filled on one side, the contour runs on the other.
    """
    solid = ink.convert('L')
    edges = ImageChops.lighter(
        solid, ImageChops.invert(solid.filter(ImageFilter.MaxFilter(3))),
    )
    if Picture.SHADOW:
        deep = gray.point(lambda v: 0 if v <= Picture.SHADOW else 255, mode='1')
        edges = ImageChops.darker(edges, deep.convert('L'))
    return edges.convert('1')


def _ink_share(bitmap: Image.Image) -> float:
    """Share of filled dots."""
    hist = bitmap.convert('L').histogram()
    black, white = hist[0], hist[255]
    total = black + white
    return black / total if total else 0.0


def _to_grayscale(img: Image.Image) -> tuple[Image.Image, Image.Image | None]:
    """To grayscale, with margins cropped and contrast stretched.

    Also returns the silhouette – the alpha channel, if there is one. A cutout
    on a transparent background (and that is half the png on the internet) gives
    no shape by itself: a gold trophy on a white background is nearly white in
    brightness, the threshold loses it, and a mess is left. The alpha, however,
    knows the shape exactly, and the contour is later traced along it.

    The transparent background is still laid on white: for the halftones it is
    background, not drawing, and it must not be set in dots.
    """
    shape = None
    if img.mode in ('RGBA', 'LA', 'P') or 'transparency' in img.info:
        img = img.convert('RGBA')
        shape = img.getchannel('A')
        canvas = Image.new('RGBA', img.size, (255, 255, 255, 255))
        img = Image.alpha_composite(canvas, img)
    img = img.convert('L')
    # Content bounds: exact from the silhouette, otherwise from non-empty pixels
    box = shape.getbbox() if shape is not None else ImageOps.invert(img).getbbox()
    if box:
        img = img.crop(box)
        if shape is not None:
            shape = shape.crop(box)
    if shape is not None and (shape.getextrema()[0] >= ALPHA_SOLID):
        # There is no actual transparency – nothing to outline
        shape = None
    return ImageOps.autocontrast(img), shape


def _add_shape(ink: Image.Image, shape: Image.Image) -> Image.Image:
    """Add an outline along the silhouette of a cutout picture."""
    solid = shape.point(lambda v: 0 if v >= ALPHA_SOLID else 255, mode='1').convert('L')
    edge = ImageChops.lighter(
        solid, ImageChops.invert(solid.filter(ImageFilter.MaxFilter(3))),
    )
    return ImageChops.darker(ink.convert('L'), edge).convert('1')


def _best_box(img_w: int, img_h: int, limit: int, max_cols: int) -> tuple[int, int]:
    """Choose a frame in cells to match the picture's proportions.

    Our budget is in message characters, not lines: a line of N characters
    costs N+1 with the separating space. So a tall picture is better off with
    fewer columns and more rows – at the same limit that yields noticeably more
    dots. max_cols caps it from above: a line wider than that will not fit into
    the chat column and will be torn by wrapping.
    """
    best = (0.0, 0, 0)
    for cols in range(MIN_COLS, max_cols + 1):
        rows = (limit + 1) // (cols + 1)
        if rows < MIN_ROWS:
            continue
        scale = min(cols * DOTS_X / img_w, rows * DOTS_Y / img_h)
        if scale > best[0]:
            best = (scale, cols, rows)
    if not best[1]:
        return max_cols, max(MIN_ROWS, (limit + 1) // (max_cols + 1))
    return best[1], best[2]


def _fit(img_w: int, img_h: int, cols: int, rows: int) -> tuple[int, int]:
    """Size in dots: fit into the frame, keeping the proportions.

    Braille dots are nearly square – a cell is twice as tall as it is wide and
    carries 2 dots horizontally against 4 vertically – so the correction for
    character proportions, mandatory for plain ASCII, is not needed here.
    """
    box_w, box_h = cols * DOTS_X, rows * DOTS_Y
    scale = min(box_w / img_w, box_h / img_h)
    w = max(DOTS_X, round(img_w * scale / DOTS_X) * DOTS_X)
    h = max(DOTS_Y, round(img_h * scale / DOTS_Y) * DOTS_Y)
    return min(w, box_w), min(h, box_h)


def _to_lines(img: Image.Image) -> list[str]:
    pixels = img.load()
    lines = []
    for top in range(0, img.height, DOTS_Y):
        line = []
        for left in range(0, img.width, DOTS_X):
            mask = 0
            for (dx, dy), bit in DOT_BITS.items():
                x, y = left + dx, top + dy
                if x < img.width and y < img.height and not pixels[x, y]:
                    mask |= bit
            line.append(chr(0x2800 + mask))
        lines.append(''.join(line))
    return lines


def _trim(lines: list[str]) -> list[str]:
    """Remove blank lines at the top and bottom – they waste the message budget."""
    while lines and not lines[0].strip(BLANK):
        lines.pop(0)
    while lines and not lines[-1].strip(BLANK):
        lines.pop()
    return lines
