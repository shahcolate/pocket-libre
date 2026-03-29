"""Summarization using Claude Haiku API. ~$0.001 per recording."""

from rich.console import Console


console = Console()

DEFAULT_MODEL = "claude-haiku-4-5-20251001"

SUMMARY_PROMPTS = {
    "meeting": """You are summarizing a meeting transcript. Be concise and direct.

Produce the following sections:

## Summary
2-3 sentences capturing the key discussion points and outcomes.

## Action Items
Bulleted list of action items. Format: "- [Owner]: [Task] by [Deadline if mentioned]"
Only include items that were clearly committed to.

## Key Decisions
Bulleted list of decisions that were made. Skip this section if none.

## Open Questions
Anything left unresolved. Skip this section if none.

Transcript:
{transcript}""",

    "notes": """You are processing voice notes. Extract the key ideas concisely.

## Key Ideas
The main thoughts captured, in order. Use bullets.

## Action Items
Any to-dos or follow-ups mentioned. Skip if none.

Transcript:
{transcript}""",

    "call": """You are summarizing a phone/video call. Be concise.

## Summary
2-3 sentences on what was discussed.

## Action Items
- [Owner]: [Task]

## Follow-ups Needed
Anything that requires a follow-up. Skip if none.

Transcript:
{transcript}""",

    "raw": """Summarize this transcript concisely. Pull out the most important information,
any action items, and any decisions made. Use markdown formatting.

Transcript:
{transcript}""",
}


def summarize_transcript(
    transcript_text: str,
    api_key: str,
    style: str = "meeting",
    model: str = DEFAULT_MODEL,
    custom_prompt: str | None = None,
) -> str:
    """Summarize a transcript using Claude Haiku.

    Args:
        transcript_text: The full transcript with speaker labels.
        api_key: Anthropic API key.
        style: One of "meeting", "notes", "call", "raw".
        model: Model to use (default: Haiku for cost efficiency).
        custom_prompt: Override the built-in prompt entirely.

    Returns:
        Markdown-formatted summary.
    """
    try:
        import anthropic
    except ImportError:
        console.print(
            "[red]anthropic package not installed.[/red] "
            "Run: [bold]pip install anthropic[/bold]"
        )
        return ""

    if custom_prompt:
        prompt = custom_prompt.replace("{transcript}", transcript_text)
    elif style in SUMMARY_PROMPTS:
        prompt = SUMMARY_PROMPTS[style].format(transcript=transcript_text)
    else:
        prompt = SUMMARY_PROMPTS["raw"].format(transcript=transcript_text)

    console.print(f"[dim]Summarizing with {model}...[/dim]")

    client = anthropic.Anthropic(api_key=api_key)

    message = client.messages.create(
        model=model,
        max_tokens=1500,
        messages=[
            {"role": "user", "content": prompt}
        ],
    )

    summary = message.content[0].text

    # Estimate cost (Haiku pricing: $0.25/MTok input, $1.25/MTok output)
    input_tokens = message.usage.input_tokens
    output_tokens = message.usage.output_tokens
    cost = (input_tokens * 0.25 + output_tokens * 1.25) / 1_000_000

    console.print(
        f"[dim]Tokens: {input_tokens} in / {output_tokens} out "
        f"(~${cost:.4f})[/dim]"
    )

    return summary


def estimate_cost(transcript_text: str) -> str:
    """Estimate API cost before calling. Returns human-readable string."""
    est_input = len(transcript_text) // 4
    est_output = 500
    cost = (est_input * 0.25 + est_output * 1.25) / 1_000_000
    return f"~${cost:.4f}"


def format_transcript_for_summary(segments: list[dict]) -> str:
    """Format labeled segments into a readable transcript string."""
    lines = []
    for seg in segments:
        timestamp = format_time(seg["start"])
        lines.append(f"[{timestamp}] {seg['speaker']}: {seg['text']}")
    return "\n".join(lines)


def format_time(seconds: float) -> str:
    """Format seconds as MM:SS."""
    m = int(seconds // 60)
    s = int(seconds % 60)
    return f"{m:02d}:{s:02d}"
