"""Chat line text between Owncast and Twitch: markup out of Owncast lines, Markdown escaped into them."""

import re
from html.parser import HTMLParser

MENTION = re.compile(r"(?<![\w@])@(\w{1,25})")


class _Text(HTMLParser):
    """Owncast message text without markup. An emoji image becomes its alt text."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_data(self, data):
        self.parts.append(data)

    def handle_starttag(self, tag, attrs):
        if tag == "img":
            alt = dict(attrs).get("alt")
            if alt:
                self.parts.append(f" {alt} ")
        elif tag in ("br", "p"):
            self.parts.append(" ")


# custom.js marks a reply with the source line id as zero-width characters between two U+2063.
# Owncast draws a reply plate from it. Twitch gets neither the mark nor a fragment left by Backspace.
# Matched only next to U+2063, since a lone U+200D joins compound emoji
REPLY_MARK = re.compile("\u2063[\u200b\u200c\u200d\u2060]*\u2063?|[\u200b\u200c\u200d\u2060]*\u2063")


def plain(body: str) -> str:
    parser = _Text()
    parser.feed(body)
    parser.close()
    text = " ".join(REPLY_MARK.sub("", "".join(parser.parts)).split())
    # A mention picked from the hint gets a line break after «@». Twitch does not highlight «@ nick»
    return re.sub(r"@\s+(?=\w)", "@", text)


# Inline markup is escaped anywhere in the line. The dot is left alone, or links break
_INLINE_MD = re.compile(r"([\\`*_\[\]<>~])")
# A twin's line starts with the viewer's own text, so a leading list, heading or rule marker would make
# it a block. Owncast drops those tags with the marker, and refuses a lone «+» or «---» with a 400
_BLOCK_START = re.compile(r"^(?:([#+=-])|(\d+)([.)]))")


def escape_markdown(text: str) -> str:
    return _INLINE_MD.sub(r"\\\1", text)


def hall_markdown(text: str) -> str:
    """A Twitch line for Owncast chat, markup escaped and @nick mentions in bold."""
    out, at = [], 0
    for m in MENTION.finditer(text):
        out += [escape_markdown(text[at : m.start()]), f"**@{escape_markdown(m.group(1))}**"]
        at = m.end()
    out.append(escape_markdown(text[at:]))
    line = "".join(out)
    return _BLOCK_START.sub(lambda m: f"\\{m[1]}" if m[1] else f"{m[2]}\\{m[3]}", line)
