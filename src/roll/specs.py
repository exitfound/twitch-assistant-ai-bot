"""The channel-points rewards as the game defines them: action, title, description, cost, limit.

The bot creates and updates them on Twitch from this list (src/twitch/roll/rewards.py): the
titles and descriptions come from CONTENT.md, the costs and limits from .env.
"""
import dataclasses

from src.core.config import Rewards, Roll
from src.core.content import Content
from src.roll import game
from src.roll.texts import curse_values, luck_values, mass_values, reward_title

# Twitch limits on reward fields
TITLE_MAX = 45
PROMPT_MAX = 200


@dataclasses.dataclass(frozen=True)
class RewardSpec:
    action: str
    cost: int
    input_required: bool
    max_per_user: int           # per stream, 0 – no limit

    @property
    def title(self) -> str:
        return reward_title(self.action)[:TITLE_MAX]

    @property
    def prompt(self) -> str:
        # The reward's description on Twitch; on a reward that takes a nick it is also
        # the caption of the input field
        return Content.text(
            f'reward_{self.action}_prompt', ceiling=Rewards.CURSE_CEILING,
            protect=Rewards.REROLL_PROTECT_MINUTES, cleanse_protect=Rewards.CLEANSE_PROTECT_MINUTES,
            shield=Rewards.SHIELD_MINUTES, flask=Rewards.FLASK_MINUTES, choke_step=Rewards.CHOKE_STEP,
            throws=Roll.BURST_THROWS, pause=Roll.BURST_PAUSE_MINUTES,
            window=Rewards.REROLL_WINDOW_MINUTES, follower=Rewards.LIMIT_FOLLOWER,
            vip=Rewards.LIMIT_VIP, mod=Rewards.LIMIT_MOD, sub=Rewards.LIMIT_SUB,
            max=Roll.MAX, **curse_values(), **luck_values(), **mass_values(),
        )[:PROMPT_MAX]

    def as_dict(self) -> dict:
        """The spec as the bot gets it: every field already rendered."""
        return {'action': str(self.action), 'title': self.title, 'prompt': self.prompt, 'cost': self.cost,
                'input_required': self.input_required, 'max_per_user': self.max_per_user}


def specs() -> list[RewardSpec]:
    """Every reward the game offers, in the order they are shown on Twitch."""
    return [
        RewardSpec(game.Action.EXTRA, Rewards.COST_EXTRA, False, Rewards.EXTRA_MAX_PER_USER),
        RewardSpec(game.Action.REROLL, Rewards.COST_REROLL, True, Rewards.REROLL_MAX_PER_USER),
        RewardSpec(game.Action.CURSE, Rewards.COST_CURSE, True, Rewards.CURSE_MAX_PER_USER),
        RewardSpec(game.Action.SHIELD, Rewards.COST_SHIELD, False, Rewards.SHIELD_MAX_PER_USER),
        RewardSpec(game.Action.CLEANSE, Rewards.COST_CLEANSE, True, Rewards.CLEANSE_MAX_PER_USER),
        RewardSpec(game.Action.FLASK, Rewards.COST_FLASK, False, Rewards.FLASK_MAX_PER_USER),
    ]
