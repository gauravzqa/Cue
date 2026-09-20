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

    # Jev thresholds. Calibrated probabilities mean these are tunable against
    # real outcomes rather than guessed -- see evals/. Defaults are deliberately
    # cautious: we would rather ask twice than act once on a mishearing.
    address_gate: float = 0.85        # P(addressed to me) to wake at all
    end_of_turn: float = 0.75         # P(user finished speaking)
    confirm_yes: float = 0.90         # P(that was a yes) to treat as consent
    confirm_no: float = 0.35          # below this, treat as a no; between = re-ask
    needs_planner: float = 0.60

    # Model selection
    voice_model: str = "deepseek-chat"
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
        )

    @property
    def jev_live(self) -> bool:
        return bool(self.typesafe_api_key)
