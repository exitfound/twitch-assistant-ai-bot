"""Chat line text between Owncast and Twitch: Owncast markup out, Markdown escaped, mentions bold."""


def test_a_mention_from_the_owncast_picker_stays_a_mention(gate):
    assert gate.chat_markup.plain("<p>@<br>\ndv4mp1r3 where are you</p>") == "@dv4mp1r3 where are you"
    assert gate.chat_markup.plain("<p>mail a @ b</p>") == "mail a @b"


def test_markdown_from_twitch_is_shown_as_text(gate):
    assert (
        gate.chat_markup.escape_markdown("a_b *c* [d](e) <b> `f` ~g~ \\ 1.5")
        == "a\\_b \\*c\\* \\[d\\](e) \\<b\\> \\`f\\` \\~g\\~ \\\\ 1.5"
    )


def test_the_reply_mark_does_not_go_to_twitch(gate):
    body = "<p>@anna\u2063\u200b\u200c\u200d\u2060\u2063 agreed</p>"
    assert gate.chat_markup.plain(body) == "@anna agreed"
    assert gate.chat_markup.plain("<p>@anna\u2063\u200b\u200c agreed</p>") == "@anna agreed"
    assert gate.chat_markup.plain("<p>@anna\u200b\u200c\u2063 agreed</p>") == "@anna agreed"
    family = "\U0001f468\u200d\U0001f469\u200d\U0001f467"
    assert gate.chat_markup.plain(f"<p>{family} family</p>") == f"{family} family"


def test_owncast_html_becomes_plain_text(gate):
    body = '<p>hello <strong>all</strong> &amp; <a href="https://t.co">https://t.co</a> <img alt=":kek:" src="x"></p>'
    assert gate.chat_markup.plain(body) == "hello all & https://t.co :kek:"


def test_mentions_are_bold_and_markdown_is_text(gate):
    assert gate.chat_markup.hall_markdown("@m1nd_sh *x* a@b.c @ ") == "**@m1nd\\_sh** \\*x\\* a@b.c @ "


def test_a_line_that_starts_like_a_block_stays_text(gate):
    cases = {
        "+": "\\+",
        "- item": "\\- item",
        "# tag": "\\# tag",
        "1. yes": "1\\. yes",
        "5) yes": "5\\) yes",
        "---": "\\---",
        "=": "\\=",
        "+1 to that": "\\+1 to that",
        "see 1. above": "see 1. above",
    }
    assert {k: gate.chat_markup.hall_markdown(k) for k in cases} == cases
