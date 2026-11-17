"""AI-powered analysis pipeline for recordings.

Each analysis type runs a single call to the configured model (llm.py;
about $0.001 with Claude Haiku) against the transcript and produces
structured output (JSON or Markdown).

Usage:
    results = run_analyses(transcript, api_key, ["summary", "entities", "mind_map"])
    # results["entities"] -> dict with people, topics, action_items, etc.
    # results["mind_map"] -> dict with topic, branches
"""

import json

from rich.console import Console

from pocket_libre.llm import as_llm, extract_json

console = Console()

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
    api_key,
    analysis_type: str,
    custom_prompt: str | None = None,
    custom_name: str | None = None,
    model: str | None = None,
) -> dict | str:
    """Run a single analysis on a transcript.

    Returns parsed JSON dict for structured types, or Markdown string for
    summary/custom types.
    """
    llm = as_llm(api_key)

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

    reply = llm.complete(prompt, system=system, max_tokens=2000, model=model)
    text = reply.text
    console.print(f"[dim]{analysis_type}: {llm.usage(reply)}[/dim]")

    if output_format == "json":
        # Models may wrap the JSON in a code fence or a sentence.
        value = extract_json(text)
        if isinstance(value, dict):
            return value
        console.print(f"[yellow]Warning: {analysis_type} returned non-JSON, saving as raw text[/yellow]")
        return {"_raw": text}

    return text


def run_analyses(
    transcript: str,
    api_key,
    types: list[str],
    custom_prompts: list[dict] | None = None,
    model: str | None = None,
) -> dict[str, dict | str]:
    """Run multiple analyses on a transcript.

    Args:
        transcript: Full transcript text
        api_key: The model to use: an llm.LLM, or an Anthropic API key
        types: List of analysis types ("mind_map", "entities", "key_quotes")
        custom_prompts: List of {"name": "...", "prompt": "..."} dicts
        model: Overrides the LLM's model

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
    api_key,
    model: str | None = None,
) -> str:
    """Ask a question about a recording's transcript.

    Returns the AI response as a string.
    """
    llm = as_llm(api_key)
    reply = llm.complete(
        model=model,
        max_tokens=1000,
        system=(
            "You answer questions about a recording transcript. Be concise and "
            "cite timestamps [MM:SS] when referencing specific parts. If the answer "
            "isn't in the transcript, say so."
        ),
        prompt=f"Transcript:\n{transcript}\n\nQuestion: {question}",
    )
    console.print(f"[dim]Chat: {llm.usage(reply)}[/dim]")
    return reply.text
