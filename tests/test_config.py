"""Configuration parsing: bad values fall back to something the bot can run with."""
import pytest

from src.core import config
from src.core.utils import safe_format


def test_swapped_roll_bounds_are_put_right(monkeypatch):
    """ROLL_MIN above ROLL_MAX made every throw crash in randint()."""
    monkeypatch.setenv('ROLL_MIN', '50')
    monkeypatch.setenv('ROLL_MAX', '10')
    assert config._env_ordered(('ROLL_MIN', 1, 1, 1000), ('ROLL_MAX', 100, 2, 1000)) == (10, 50)


def test_bounds_in_order_are_kept_and_out_of_range_ones_fall_back(monkeypatch):
    monkeypatch.setenv('EMOTE_SPAM_MIN', '2')
    monkeypatch.setenv('EMOTE_SPAM_MAX', '99')
    assert config._env_ordered(('EMOTE_SPAM_MIN', 1, 1, 20), ('EMOTE_SPAM_MAX', 5, 1, 20)) == (2, 5)


def test_zero_curse_step_is_refused(monkeypatch):
    """With a step of 0 the ceiling never reaches the floor and a curse lasts all stream."""
    monkeypatch.setenv('REWARD_CURSE_STEP', '0')
    assert config._curse_step() == 10


def test_attribute_access_in_a_template_does_not_crash():
    assert safe_format('@{user.name}', user='gop') == '@{user.name}'


def test_unknown_time_zone_stops_the_start(monkeypatch):
    """An unknown zone must not fall back to UTC: every session id would move by hours."""
    monkeypatch.setenv('BOT_TIMEZONE', 'Europe/Kiev_typo')
    with pytest.raises(ValueError, match='BOT_TIMEZONE'):
        config._env_zone('BOT_TIMEZONE', 'Europe/Moscow')
    monkeypatch.delenv('BOT_TIMEZONE')
    assert config._env_zone('BOT_TIMEZONE', 'Europe/Moscow').key == 'Europe/Moscow'
