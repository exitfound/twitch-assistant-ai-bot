class ContextBuilder:
    """Builds a structured prompt string from named sections.

    Each section is rendered as:
        [Label]
        content

    Sections with no label (add_raw, add_prompt) are rendered as plain text.
    Empty data is silently skipped.
    """

    def __init__(self) -> None:
        self._sections: list[tuple[str | None, str]] = []

    def add_facts(self, facts: list[tuple[str, str]],
                  label: str = 'Сохранённые факты') -> 'ContextBuilder':
        if facts:
            lines = '\n'.join(f'{u}: {f}' for u, f in facts)
            self._sections.append((label, lines))
        return self

    def add_chat(self, messages: list[tuple[str, str]],
                 label: str = 'Последние сообщения в чате') -> 'ContextBuilder':
        if messages:
            lines = '\n'.join(f'{u}: {m}' for u, m in messages)
            self._sections.append((label, lines))
        return self

    def add_lines(self, label: str, lines: list[str]) -> 'ContextBuilder':
        if lines:
            self._sections.append((label, '\n'.join(lines)))
        return self

    def add_user_messages(self, label: str, messages: list[str]) -> 'ContextBuilder':
        if messages:
            self._sections.append((label, '\n'.join(messages)))
        return self

    def add_interactions(self, label: str, username: str,
                         interactions: list[tuple[str, str]]) -> 'ContextBuilder':
        if interactions:
            lines = '\n'.join(f'{username}: {q} → бот: {a}' for q, a in interactions)
            self._sections.append((label, lines))
        return self

    def add_prompt(self, user: str, prompt: str) -> 'ContextBuilder':
        self._sections.append((None, f'{user} спрашивает: {prompt}'))
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
