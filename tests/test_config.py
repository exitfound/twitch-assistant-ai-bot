"""Configuration parsing: bad values fall back to something the bot can run with."""
from src.core import config
from src.core.utils import safe_format


def test_swapped_roll_bounds_are_put_right(monkeypatch):
    """ROLL_MIN above ROLL_MAX made every throw crash in randint()."""
    monkeypatch.setenv('ROLL_MIN', '50')
    monkeypatch.setenv('ROLL_MAX', '10')
    assert config._roll_range() == (10, 50)


def test_curse_bounds_are_put_right(monkeypatch):
    monkeypatch.setenv('REWARD_CURSE_CEILING', '20')
    monkeypatch.setenv('REWARD_CURSE_FLOOR', '60')
    assert config._curse_range() == (60, 20)


def test_zero_curse_step_is_refused(monkeypatch):
    """With a step of 0 the ceiling never reaches the floor and a curse lasts all stream."""
    monkeypatch.setenv('REWARD_CURSE_STEP', '0')
    assert config._curse_step() == 5


def test_attribute_access_in_a_template_does_not_crash():
    assert safe_format('@{user.name}', user='gop') == '@{user.name}'
