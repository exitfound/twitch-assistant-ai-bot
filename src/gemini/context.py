class ContextBuilder:
    """Собирает промпт для Gemini из именованных секций.

    Каждая секция рендерится как:
        [Label]
        content

    Секции без метки (add_raw) — просто текст. Пустые данные пропускаются.

    Класс отвечает только за структуру: все русские формулировки, включая
    названия меток, приходят из CONTENT.md — prompts.system ссылается
    на них по названию, поэтому они должны лежать рядом.
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
