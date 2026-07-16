"""Model pricing table and cost helpers (USD).

Prices are USD per million tokens, cached from the Anthropic pricing docs
(2026-06): Fable/Mythos 5 $10/$50, Opus 4.x $5/$25, Sonnet 4.x/5 $3/$15,
Haiku 4.5 $1/$5. (Sonnet 5 has a lower introductory price through 2026-08-31;
the sticker price is used here so estimates stay valid after it ends.)

Cache reads bill at ~0.1x the input price; cache writes at 1.25x (5-minute
TTL) or 2x (1-hour TTL). Claude Code writes with the 1-hour TTL (the
docs/log-format.md sample shows ephemeral_1h_input_tokens), so when a turn
has no 5m/1h split recorded, cache_creation is priced as 1-hour writes.

For subscription (Max plan) users these dollar figures are notional — but
they remain the right relative measure of usage-limit pressure.
"""

from dataclasses import dataclass
from typing import Optional

CACHE_READ_FACTOR = 0.1
CACHE_WRITE_5M_FACTOR = 1.25
CACHE_WRITE_1H_FACTOR = 2.0


@dataclass(frozen=True)
class ModelPricing:
    input_per_mtok: float
    output_per_mtok: float


PRICING = {
    "claude-fable-5": ModelPricing(10.0, 50.0),
    "claude-mythos-5": ModelPricing(10.0, 50.0),
    "claude-opus-4-8": ModelPricing(5.0, 25.0),
    "claude-opus-4-7": ModelPricing(5.0, 25.0),
    "claude-opus-4-6": ModelPricing(5.0, 25.0),
    "claude-opus-4-5": ModelPricing(5.0, 25.0),
    "claude-opus-4-1": ModelPricing(5.0, 25.0),
    "claude-sonnet-5": ModelPricing(3.0, 15.0),
    "claude-sonnet-4-6": ModelPricing(3.0, 15.0),
    "claude-sonnet-4-5": ModelPricing(3.0, 15.0),
    "claude-haiku-4-5": ModelPricing(1.0, 5.0),
}


def canonical(model: Optional[str]) -> Optional[str]:
    """Map a model ID (possibly with a date suffix, e.g.
    claude-haiku-4-5-20251001) to its PRICING key, or None if unknown."""
    if not model:
        return None
    for known in PRICING:
        if model == known or model.startswith(known + "-"):
            return known
    return None


def lookup(model: Optional[str]) -> Optional[ModelPricing]:
    key = canonical(model)
    return PRICING[key] if key else None


def turn_cost_usd(
    model: Optional[str],
    *,
    input_tokens: int = 0,
    output_tokens: int = 0,
    cache_read: int = 0,
    cache_creation: int = 0,
    cache_5m: int = 0,
    cache_1h: int = 0,
) -> Optional[float]:
    """Price a token profile at `model`'s rates. Returns None for unknown
    models. If no 5m/1h split is given, cache_creation is billed as 1-hour
    writes (Claude Code's TTL)."""
    p = lookup(model)
    if p is None:
        return None
    if cache_5m + cache_1h == 0 and cache_creation:
        cache_1h = cache_creation
    usd = (
        input_tokens * p.input_per_mtok
        + output_tokens * p.output_per_mtok
        + cache_read * p.input_per_mtok * CACHE_READ_FACTOR
        + cache_5m * p.input_per_mtok * CACHE_WRITE_5M_FACTOR
        + cache_1h * p.input_per_mtok * CACHE_WRITE_1H_FACTOR
    ) / 1_000_000
    return usd
