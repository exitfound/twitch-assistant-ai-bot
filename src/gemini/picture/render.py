"""Pixels to braille characters: a picture that fits into one chat message.

Twitch chat is drawn in a proportional font, where `.:-=+*#%@` lines drift apart and
runs of spaces collapse. Braille (U+2800–U+28FF) renders at one width, its blank
U+2800 is not a space and does not collapse, and one cell carries a 2x4 dot matrix.

Everything goes out as one message: a braille line holds no spaces, so a browser treats
it as one unbreakable word and wraps only at the joins between lines, which puts each
line on its own visual line without the newlines a Twitch message cannot carry. Hence
the size budget is in message characters, not in lines.
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

# Share of filled dots above which the picture counts as flooded: one threshold for
# the whole picture fails when a large area lies just slightly darker than it, turning
# a green frog into a faceless silhouette. A local threshold takes over
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

# Longest side a picture is shrunk to right after decoding. The art is at most 52 dots
# wide and the preview 512 px, so nothing is lost, while every later step – colour
# conversion, compositing, crop – works on a copy that is megabytes, not hundreds of them
WORK_PX = 1024


def render(data: bytes, *, limit: int, max_cols: int) -> str | None:
    """A finished chat message, or None if drawing failed.

    A blocking function: decoding and resizing are CPU-bound,
    so it should be called via asyncio.to_thread.
    """
    img = _open(data)
    if img is None:
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

    A small picture is enough to say what is drawn and whether it may be shown, while
    an original phone photo weighs megabytes and costs accordingly.
    """
    img = _open(data)
    if img is None:
        return None
    try:
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


def _open(data: bytes) -> Image.Image | None:
    """Decode a picture, shrunk to WORK_PX before anything else touches it.

    The size is known from the header and is checked BEFORE decoding: a hundred-kilobyte
    png expands into hundreds of megapixels, and decoding would eat the memory before
    there is any chance to refuse. A JPEG is decoded at a reduced scale right away (draft).
    """
    try:
        img = Image.open(io.BytesIO(data))
        if img.width * img.height > MAX_PIXELS:
            logger.info('!ascii: картинка слишком большая: %dx%d', img.width, img.height)
            return None
        img.draft(img.mode, (WORK_PX, WORK_PX))
        img.load()
        if max(img.size) > WORK_PX:
            img.thumbnail((WORK_PX, WORK_PX), Image.LANCZOS)
    except Exception:
        logger.info('!ascii: файл не открылся как картинка')
        return None
    return img


def _binarize(img: Image.Image) -> Image.Image:
    """Halftones to black and white, already at the final size.

    A plain threshold comes first, since it gives the cleanest lines on a high-contrast
    drawing. When the picture floods or nearly disappears, a local threshold takes over
    and compares a dot with its surroundings, pulling outlines out of large flat areas.
    Dithering is useless here: at sixty-odd dots across Floyd-Steinberg gives noise.
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

    A solid black blob loses its shape, so the drawing eroded by one dot is subtracted
    from itself and only the edges remain; thin lines are edge themselves and survive
    untouched. The darkest places (darker than `Picture.SHADOW`) are put back on top so
    the result is not a wire diagram: shadow filled on one side, contour on the other.
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

    Also returns the silhouette, the alpha channel where there is one: a cutout on a
    transparent background gives no shape by brightness alone – a gold trophy is nearly
    white and the threshold loses it – while the alpha knows the shape exactly and the
    contour is traced along it. The transparent background is still laid on white,
    because for the halftones it is background and must not be set in dots.
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

    The budget is in message characters, not lines: a line of N characters costs N+1
    with the separating space, so a tall picture gains dots from fewer columns and more
    rows. max_cols is the ceiling – a wider line will not fit the chat column and is
    torn by wrapping.
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
