"""Model selection and cost estimation for Anthropic API calls.

Rates are per million tokens. Keeping them here means a price or model
change is a one-line edit rather than a hunt through every call site.
"""

# Haiku 4.5 — cheapest model that handles summarization and extraction well.
DEFAULT_MODEL = "claude-haiku-4-5"

# USD per million tokens.
INPUT_PRICE_PER_MTOK = 1.00
OUTPUT_PRICE_PER_MTOK = 5.00


def cost_usd(input_tokens: int, output_tokens: int) -> float:
    """Return the USD cost of a call with the given token counts."""
    return (
        input_tokens * INPUT_PRICE_PER_MTOK
        + output_tokens * OUTPUT_PRICE_PER_MTOK
    ) / 1_000_000


def format_cost(input_tokens: int, output_tokens: int) -> str:
    """Human-readable token and cost summary for console output."""
    return (
        f"{input_tokens:,}+{output_tokens:,} tokens "
        f"(~${cost_usd(input_tokens, output_tokens):.4f})"
    )
