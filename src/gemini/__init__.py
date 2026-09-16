"""Всё, что стоит запроса к Gemini.

    client.py     клиент, generate() с повторами, make_gen_config()
    context.py    ContextBuilder — сборка промпта из секций
    responder.py  конвейер ответа: очистка, стоп-лист, CAPS, эмот, отправка
    commands.py   !ask, !summary, !who, !versus и свободное обращение к боту
    proactive.py  реплики бота от себя раз в интервал

Новая команда с генерацией — сюда, при регистрации с kind=KIND_GEMINI.
"""
