class ContextBuilder:
    """Assembles a Gemini prompt from named sections.

    Each section renders as:
        [Label]
        content

    Sections without a label (add_raw) are plain text. Empty data is skipped.

    The class is responsible for structure only: all Russian wording, including
    the label names, comes from CONTENT.md – prompts.system refers to them
    by name, so they have to live side by side.
    """

    def __init__(self) -> None:
        self._sections: list[tuple[str | None, str]] = []

    def add_facts(self, label: str, facts: list[tuple[str, str]]) -> 'ContextBuilder':
        if facts:
            self._sections.append((label, '\n'.join(f'{u}: {f}' for u, f in facts)))
        return self

    def add_chat(self, label: str, messages: list[tuple[str, str]]) -> 'ContextBuilder':
        if messages:
            self._sections.append((label, '\n'.join(f'{u}: {m}' for u, m in messages)))
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
