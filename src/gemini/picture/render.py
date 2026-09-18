"""Пиксели в символы Брайля: картинка, влезающая в одно сообщение чата.

Почему брайль, а не обычный ASCII. Чат Twitch рисуется пропорциональным
шрифтом, поэтому строки из `.:-=+*#%@` разъезжаются, а подряд идущие пробелы
схлопываются – половина арта просто исчезает. Символы Брайля
(U+2800–U+28FF) рендерятся одинаковой ширины, пустой символ U+2800 – не
пробел и не схлопывается, а плотность вчетверо выше: одна ячейка несёт
матрицу 2x4 точки.

Почему всё уходит одним сообщением. Строка брайля не содержит пробелов,
для браузера это одно неразрывное «слово». Пробелы стоят только на стыках
строк, перенос идёт строго по ним – значит каждая строка ложится на свою
визуальную строку сама, без переводов строки, которых в сообщении Twitch и
не бывает. Ник отправителя не мешает: первая строка арта целиком не влезает
в остаток первой визуальной строки и уезжает на вторую.

Отсюда и размер: бюджет не в строках, а в символах сообщения.
"""
import io
import logging

from PIL import Image, ImageChops, ImageFilter, ImageOps

from src.core.config import Picture

logger = logging.getLogger(__name__)

# Символ Брайля – это 0x2800 плюс маска из восьми точек. Точки в ячейке 2x4
# пронумерованы не по порядку чтения, а исторически: слева 1,2,3,7, справа 4,5,6,8
DOT_BITS = {
    (0, 0): 0x01, (0, 1): 0x02, (0, 2): 0x04, (0, 3): 0x40,
    (1, 0): 0x08, (1, 1): 0x10, (1, 2): 0x20, (1, 3): 0x80,
}
BLANK = '⠀'
DOTS_X = 2
DOTS_Y = 4

# Границы подбора рамки: уже – уже ничего не разобрать, ниже – не картинка
MIN_COLS = 14
MIN_ROWS = 6

# Порог яркости, ниже которого точка считается закрашенной
THRESHOLD = 128

# Доля закрашенных точек, выше которой картинка считается залитой. Порог
# один на всю картинку не справляется, когда большая область лежит чуть
# темнее него: зелёная лягушка целиком уходит в чернила и остаётся силуэт
# без морды. Тогда переключаемся на локальный порог
INK_MAX = 0.45
# И наоборот: почти пустой результат тоже значит, что порог не туда попал
INK_MIN = 0.05

# Локальный порог: пиксель темнее своей округи на BIAS – чернила. Радиус
# в точках готовой картинки, поэтому маленький
LOCAL_RADIUS = 2.0
LOCAL_BIAS = 6

# Пиксель считается частью силуэта, если он непрозрачен хотя бы наполовину
ALPHA_SOLID = 128

# Размер копии, которая уходит в Gemini: ему надо лишь понять, что на
# картинке, и решить, можно ли её показывать – разрешение на это не влияет
PREVIEW_PX = 512
PREVIEW_QUALITY = 85

# Потолок на распакованный размер: сжатая картинка в пару мегабайт
# разворачивается в сотни мегапикселей, и декодирование съест память
MAX_PIXELS = 40_000_000


def render(data: bytes, *, limit: int, max_cols: int) -> str | None:
    """Готовое сообщение для чата или None, если нарисовать не вышло.

    Блокирующая функция: декодирование и ресайз упираются в процессор,
    поэтому вызывать её следует через asyncio.to_thread.
    """
    try:
        img = Image.open(io.BytesIO(data))
        # Размер известен уже из заголовка, и проверять его надо ДО load():
        # стокилобайтный png разворачивается в сотни мегапикселей, и
        # декодирование съест память раньше, чем мы успеем отказать
        if img.width * img.height > MAX_PIXELS:
            logger.info('!ascii: картинка слишком большая: %dx%d', img.width, img.height)
            return None
        img.load()
    except Exception:
        logger.info('!ascii: файл не открылся как картинка')
        return None

    img, shape = _to_grayscale(img)
    # Ячейка в чате выше, чем вдвое своей ширины, поэтому точка не квадратная:
    # без поправки картинка выглядит вытянутой по вертикали. Сжимаем заранее,
    # считая геометрию по «экранной» высоте
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
    """Уменьшенная копия для Gemini. Блокирующая, звать через to_thread.

    Модели нужно ответить, что нарисовано и можно ли это показывать, – для
    этого хватает мелкой картинки. Слать оригинал незачем: снимок с телефона
    весит мегабайты и стоит соответственно, а решение от разрешения не
    меняется.
    """
    try:
        img = Image.open(io.BytesIO(data))
        if img.width * img.height > MAX_PIXELS:
            return None
        img.load()
        # JPEG не умеет прозрачность, а она тут и не нужна
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
    """Полутона в чёрно-белое, уже на готовом размере.

    Сначала пробуем обычный порог: на контрастном рисунке он даёт самые
    чистые линии. Если картинка от него залилась или, наоборот, почти
    исчезла, берём локальный порог – он сравнивает точку не с общим числом,
    а с её окружением, и потому вытягивает контуры и черты внутри крупных
    однотонных пятен. Дизеринг здесь не годится вовсе: на шести десятках
    точек в ширину Флойд-Стейнберг даёт шум, а не полутона.
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
    """Выесть заливку, оставив края и самые глубокие тени.

    Сплошное чёрное пятно теряет форму: у енота на фотографии виден контур,
    а тело – клякса. Вычитаем из рисунка его же, ужатый на точку: остаются
    только края. Тонкие линии при этом целы – они сами состоят из края,
    поэтому рисованному Pepe выедание ничего не делает.

    Чтобы не осталась одна проволочная схема, поверх возвращаются самые
    тёмные места (темнее `Picture.SHADOW`) – они и дают объём: с одной
    стороны тень залита, с другой идёт контур.
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
    """Доля закрашенных точек."""
    hist = bitmap.convert('L').histogram()
    black, white = hist[0], hist[255]
    total = black + white
    return black / total if total else 0.0


def _to_grayscale(img: Image.Image) -> tuple[Image.Image, Image.Image | None]:
    """В серый, с обрезкой полей и растяжкой контраста.

    Возвращает ещё и силуэт – канал прозрачности, если он есть. Вырезанная
    картинка на прозрачном фоне (а это половина png в интернете) сама по
    себе форму не даёт: золотой кубок на белом фоне по яркости почти белый,
    порог его теряет, и остаётся каша. Зато альфа знает форму точно, и по
    ней потом обводится контур.

    Прозрачный фон при этом всё равно кладём на белое: для полутонов это
    фон, а не рисунок, и точками его ставить не надо.
    """
    shape = None
    if img.mode in ('RGBA', 'LA', 'P') or 'transparency' in img.info:
        img = img.convert('RGBA')
        shape = img.getchannel('A')
        canvas = Image.new('RGBA', img.size, (255, 255, 255, 255))
        img = Image.alpha_composite(canvas, img)
    img = img.convert('L')
    # Границы содержимого: по силуэту они точные, иначе по непустым пикселям
    box = shape.getbbox() if shape is not None else ImageOps.invert(img).getbbox()
    if box:
        img = img.crop(box)
        if shape is not None:
            shape = shape.crop(box)
    if shape is not None and (shape.getextrema()[0] >= ALPHA_SOLID):
        # Прозрачности по факту нет – обводить нечего
        shape = None
    return ImageOps.autocontrast(img), shape


def _add_shape(ink: Image.Image, shape: Image.Image) -> Image.Image:
    """Добавить обводку по силуэту вырезанной картинки."""
    solid = shape.point(lambda v: 0 if v >= ALPHA_SOLID else 255, mode='1').convert('L')
    edge = ImageChops.lighter(
        solid, ImageChops.invert(solid.filter(ImageFilter.MaxFilter(3))),
    )
    return ImageChops.darker(ink.convert('L'), edge).convert('1')


def _best_box(img_w: int, img_h: int, limit: int, max_cols: int) -> tuple[int, int]:
    """Подобрать рамку в ячейках под пропорции картинки.

    Бюджет у нас в символах сообщения, а не в строках: строка из N символов
    стоит N+1 вместе с пробелом-разделителем. Поэтому высокой картинке
    выгоднее взять колонок меньше, а строк больше – точек при том же лимите
    выходит заметно больше. Сверху ограничивает max_cols: строка шире него
    не влезет в колонку чата и порвётся переносом.
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
    """Размер в точках: вписать в рамку, сохранив пропорции.

    Точки брайля почти квадратные – ячейка вдвое выше своей ширины и несёт
    2 точки по горизонтали против 4 по вертикали, – поэтому поправка на
    пропорции символа, обязательная для обычного ASCII, здесь не нужна.
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
    """Убрать пустые строки сверху и снизу – они тратят бюджет сообщения."""
    while lines and not lines[0].strip(BLANK):
        lines.pop(0)
    while lines and not lines[-1].strip(BLANK):
        lines.pop()
    return lines
