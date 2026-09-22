"""Process-wide settings. Read once, frozen, injected — never re-read from os.environ
deeper in the stack, so a test can construct a Settings() without monkeypatching env."""

from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv


def _flag(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    return default if raw is None else raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True, slots=True)
class Settings:
    typesafe_api_key: str | None = None
    deepseek_api_key: str | None = None

    # Feature flags
    always_on: bool = False
    dry_run: bool = True
    stt_local: bool = True

    # Capability switches. OFF by default, and that is not timidity: each of
    # these adds a class of action the model can reach for, and a capability
    # that switches itself on because a package happens to be importable is a
    # capability nobody decided to have. Turning one on is a sentence the user
    # says once, in a file, deliberately.
    enable_browser: bool = False
    enable_computer_use: bool = False

    # The agent loop's bounds. daa asks, runs ONE step, feeds the result back
    # and asks again; without these it would do that until the model got bored.
    # Eight steps is enough for "open the page, find the control, press it,
    # check it took" with room for one recovery, and short enough that a model
    # stuck in a rut costs the user seconds rather than minutes. The wall clock
    # is the one that catches a step that BLOCKS -- a confirmation nobody
    # answers, a page that never loads -- which the step count never sees.
    agent_max_steps: int = 8
    agent_max_seconds: float = 45.0

    # Grants and jobs. Short on purpose -- a grant is a bounded bargain, and
    # the cheapest way for one to become blanket permission is to outlive the
    # situation the user was picturing when they agreed to it.
    grant_ttl_s: float = 300.0
    warrant_ttl_s: float = 30.0
    notice_quiet_s: float = 20.0
    max_jobs: int = 1

    # Jev thresholds. Calibrated probabilities mean these are tunable against
    # real outcomes rather than guessed -- see evals/. Defaults are deliberately
    # cautious: we would rather ask twice than act once on a mishearing.
    # 0.42 sits in the band between the highest-scoring unaddressed utterance
    # (0.39, "i was telling daa to open it") and the second-lowest addressed one
    # (0.43). At this value evals/ scores 1 miss and 0 false wakes on 40 cases.
    # It was 0.85, which was a guess and cost 13 of 16 misses. CAVEAT: the band
    # is only 0.04 wide on 40 samples -- widen the dataset before trusting it.
    address_gate: float = 0.42        # P(addressed to me) to wake at all
    end_of_turn: float = 0.75         # P(user finished speaking)
    confirm_yes: float = 0.90         # P(that was a yes) to treat as consent
    confirm_no: float = 0.35          # below this, treat as a no; between = re-ask
    needs_planner: float = 0.60
    # Sits exactly at maximum uncertainty, and that is the whole idea: daa
    # halts on a coin flip. Every other threshold here demands better-than-even
    # evidence before DOING something; this one demands better-than-even
    # evidence before CONTINUING. A false stop costs a repeat, a missed stop
    # means the thing the user is trying to halt carries on regardless.
    stop_p: float = 0.50

    # Model selection
    # deepseek-flash, not deepseek-chat: `models.list()` on a real key returns
    # exactly {deepseek-flash, deepseek-v4-pro} and no deepseek-chat, so the
    # old default 404s. Flash is also the right half of the pair here -- the
    # voice loop wants latency, and the slow model belongs behind the
    # needs_planner gate, not on the conversational path.
    voice_model: str = "deepseek-flash"
    deepseek_base_url: str = "https://api.deepseek.com/v1"

    @classmethod
    def load(cls) -> Settings:
        load_dotenv(".env")
        return cls(
            typesafe_api_key=os.environ.get("TYPESAFE_API_KEY") or None,
            deepseek_api_key=os.environ.get("DEEPSEEK_API_KEY") or None,
            always_on=_flag("DAA_ALWAYS_ON", False),
            dry_run=_flag("DAA_DRY_RUN", True),
            stt_local=_flag("DAA_STT_LOCAL", True),
            enable_browser=_flag("DAA_ENABLE_BROWSER", False),
            enable_computer_use=_flag("DAA_ENABLE_COMPUTER_USE", False),
        )

    @property
    def jev_live(self) -> bool:
        return bool(self.typesafe_api_key)
