"""AI-powered analysis pipeline for recordings.

Each analysis type runs a single Claude Haiku call (~$0.001) against the
transcript and produces structured output (JSON or Markdown).

Usage:
    results = run_analyses(transcript, api_key, ["summary", "entities", "mind_map"])
    # results["entities"] -> dict with people, topics, action_items, etc.
    # results["mind_map"] -> dict with topic, branches
"""

import json

from rich.console import Console


console = Console()

DEFAULT_MODEL = "claude-haiku-4-5-20251001"

# ── Analysis Prompts ───────────────────────────────

ANALYSIS_PROMPTS = {
    "mind_map": {
        "system": "You extract the structure of conversations as mind maps. Return ONLY valid JSON, no markdown.",
        "user": """Analyze this transcript and create a mind map structure.

Return JSON in this exact format:
{{
  "topic": "Main topic of the recording (short title)",
  "branches": [
    {{
      "label": "Branch topic",
      "children": ["Leaf 1", "Leaf 2", "Leaf 3"]
    }}
  ]
}}

Keep branch labels short (2-4 words). Keep leaves concise (one line each).
Aim for 3-6 branches with 2-5 leaves each.

Transcript:
{transcript}""",
        "format": "json",
    },

    "entities": {
        "system": "You extract structured information from transcripts. Return ONLY valid JSON, no markdown.",
        "user": """Extract all notable entities and items from this transcript.

Return JSON in this exact format:
{{
  "people": ["Name or role of each person mentioned"],
  "organizations": ["Companies, teams, or groups mentioned"],
  "topics": ["Key themes or subjects discussed"],
  "action_items": [
    {{"owner": "Person", "task": "What they need to do", "deadline": "If mentioned, else null"}}
  ],
  "decisions": ["Decisions that were made"],
  "dates": ["Any dates or deadlines mentioned with context"],
  "key_quotes": [
    {{"speaker": "Who said it", "quote": "What they said", "timestamp": "MM:SS if available"}}
  ]
}}

Only include items actually present in the transcript. Use empty arrays for missing categories.

Transcript:
{transcript}""",
        "format": "json",
    },

    "key_quotes": {
        "system": "You identify the most important and memorable quotes from conversations. Return ONLY valid JSON, no markdown.",
        "user": """Pick the 3-8 most important or memorable quotes from this transcript.

Return JSON in this exact format:
{{
  "quotes": [
    {{
      "speaker": "Who said it",
      "quote": "The exact or near-exact quote",
      "timestamp": "MM:SS",
      "context": "Brief context for why this matters (1 sentence)"
    }}
  ]
}}

Focus on: decisions made, commitments given, surprising insights, strong opinions.

Transcript:
{transcript}""",
        "format": "json",
    },
}


def analyze_transcript(
    transcript: str,
    api_key: str,
    analysis_type: str,
    custom_prompt: str | None = None,
    custom_name: str | None = None,
    model: str = DEFAULT_MODEL,
) -> dict | str:
    """Run a single analysis on a transcript.

    Returns parsed JSON dict for structured types, or Markdown string for
    summary/custom types.
    """
    try:
        import anthropic
    except ImportError:
        console.print("[red]anthropic package not installed. Run: pip install anthropic[/red]")
        return {} if analysis_type != "custom" else ""

    client = anthropic.Anthropic(api_key=api_key)

    if analysis_type == "custom" and custom_prompt:
        prompt = custom_prompt.replace("{transcript}", transcript)
        system = "You analyze transcripts based on user instructions. Be concise and helpful."
        output_format = "markdown"
    elif analysis_type in ANALYSIS_PROMPTS:
        cfg = ANALYSIS_PROMPTS[analysis_type]
        system = cfg["system"]
        prompt = cfg["user"].format(transcript=transcript)
        output_format = cfg["format"]
    else:
        return {} if analysis_type != "custom" else ""

    message = client.messages.create(
        model=model,
        max_tokens=2000,
        system=system,
        messages=[{"role": "user", "content": prompt}],
    )

    text = message.content[0].text

    # Log cost
    input_tokens = message.usage.input_tokens
    output_tokens = message.usage.output_tokens
    cost = (input_tokens * 0.25 + output_tokens * 1.25) / 1_000_000
    console.print(f"[dim]{analysis_type}: {input_tokens}+{output_tokens} tokens (~${cost:.4f})[/dim]")

    if output_format == "json":
        # Extract JSON from response (Claude may wrap it in markdown code blocks)
        cleaned = text.strip()
        if cleaned.startswith("```"):
            # Strip markdown code fence
            lines = cleaned.split("\n")
            lines = [l for l in lines if not l.strip().startswith("```")]
            cleaned = "\n".join(lines)
        try:
            return json.loads(cleaned)
        except json.JSONDecodeError:
            console.print(f"[yellow]Warning: {analysis_type} returned non-JSON, saving as raw text[/yellow]")
            return {"_raw": text}

    return text


def run_analyses(
    transcript: str,
    api_key: str,
    types: list[str],
    custom_prompts: list[dict] | None = None,
    model: str = DEFAULT_MODEL,
) -> dict[str, dict | str]:
    """Run multiple analyses on a transcript.

    Args:
        transcript: Full transcript text
        api_key: Anthropic API key
        types: List of analysis types ("mind_map", "entities", "key_quotes")
        custom_prompts: List of {"name": "...", "prompt": "..."} dicts
        model: Claude model to use

    Returns:
        Dict mapping analysis type name to result (JSON dict or Markdown string)
    """
    results = {}

    for analysis_type in types:
        if analysis_type == "summary":
            continue  # Summary is handled by summarize.py
        try:
            result = analyze_transcript(transcript, api_key, analysis_type, model=model)
            if result:
                results[analysis_type] = result
        except Exception as e:
            console.print(f"[yellow]{analysis_type} analysis failed: {e}[/yellow]")

    # Run custom prompts
    if custom_prompts:
        for cp in custom_prompts:
            name = cp.get("name", "custom")
            prompt = cp.get("prompt", "")
            if not prompt:
                continue
            try:
                result = analyze_transcript(
                    transcript, api_key, "custom",
                    custom_prompt=prompt, custom_name=name, model=model,
                )
                if result:
                    results[f"custom_{name}"] = result
            except Exception as e:
                console.print(f"[yellow]Custom analysis '{name}' failed: {e}[/yellow]")

    return results


def save_analyses(results: dict[str, dict | str], rec_dir, timestamp: str):
    """Save analysis results to files alongside the recording."""
    from pathlib import Path
    rec_dir = Path(rec_dir)

    for name, data in results.items():
        if isinstance(data, dict):
            path = rec_dir / f"{timestamp}_{name}.json"
            path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        else:
            path = rec_dir / f"{timestamp}_{name}.md"
            path.write_text(str(data), encoding="utf-8")


def load_analyses(rec_dir, timestamp: str) -> dict[str, dict | str]:
    """Load all analysis results for a recording."""
    from pathlib import Path
    rec_dir = Path(rec_dir)
    results = {}

    for path in rec_dir.glob(f"{timestamp}_*.json"):
        name = path.stem.replace(f"{timestamp}_", "")
        if name in ("transcript",):
            continue
        try:
            results[name] = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass

    for path in rec_dir.glob(f"{timestamp}_custom_*.md"):
        name = path.stem.replace(f"{timestamp}_", "")
        try:
            results[name] = path.read_text(encoding="utf-8")
        except OSError:
            pass

    return results


def chat_with_recording(
    transcript: str,
    question: str,
    api_key: str,
    model: str = DEFAULT_MODEL,
) -> str:
    """Ask a question about a recording's transcript.

    Returns the AI response as a string.
    """
    try:
        import anthropic
    except ImportError:
        return "Error: anthropic package not installed."

    client = anthropic.Anthropic(api_key=api_key)

    message = client.messages.create(
        model=model,
        max_tokens=1000,
        system=(
            "You answer questions about a recording transcript. Be concise and "
            "cite timestamps [MM:SS] when referencing specific parts. If the answer "
            "isn't in the transcript, say so."
        ),
        messages=[{
            "role": "user",
            "content": f"Transcript:\n{transcript}\n\nQuestion: {question}",
        }],
    )

    cost = (message.usage.input_tokens * 0.25 + message.usage.output_tokens * 1.25) / 1_000_000
    console.print(f"[dim]Chat: {message.usage.input_tokens}+{message.usage.output_tokens} tokens (~${cost:.4f})[/dim]")

    return message.content[0].text
