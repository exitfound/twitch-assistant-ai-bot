Chat = list[tuple[str, str]]


def chat_chars(pairs: Chat) -> int:
    """Characters the pairs take in a prompt: «author: text» plus the line break."""
    return sum(len(u) + len(t) + 3 for u, t in pairs)


def tail_within(pairs: Chat, budget: int) -> Chat:
    """The latest pairs that fit in budget characters, oldest first."""
    start = len(pairs)
    while start:
        size = chat_chars(pairs[start - 1:start])
        if size > budget:
            break
        budget -= size
        start -= 1
    return pairs[start:]


class ContextBuilder:
    """Assembles a Gemini prompt from named sections.

    Each section renders as:
        [Label]
        content

    Sections without a label (add_raw, add_pairs with None) are plain text. Empty data is
    skipped.

    The class is responsible for structure only: all Russian wording, including
    the label names, comes from CONTENT.md – prompts.system refers to them
    by name, so they have to live side by side.
    """

    def __init__(self) -> None:
        self._sections: list[tuple[str | None, str]] = []

    def add_pairs(self, label: str | None, pairs: list[tuple[str, str]]) -> 'ContextBuilder':
        """One «author: text» line per pair – chat messages or facts. No label – no heading."""
        if pairs:
            self._sections.append((label, '\n'.join(f'{u}: {t}' for u, t in pairs)))
        return self

    def add_lines(self, label: str, lines: list[str]) -> 'ContextBuilder':
        if lines:
            self._sections.append((label, '\n'.join(lines)))
        return self

    def add_raw(self, text: str) -> 'ContextBuilder':
        self._sections.append((None, text))
        return self

    def build(self) -> str:
        return self._render(skip_labels=())

    def build_without(self, *labels: str) -> str:
        return self._render(skip_labels=labels)

    def _render(self, skip_labels: tuple) -> str:
        parts = []
        for label, content in self._sections:
            if label in skip_labels:
                continue
            parts.append(f'[{label}]\n{content}' if label else content)
        return '\n\n'.join(parts)
