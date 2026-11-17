"""The language model behind summaries, analyses, chat and speaker labels.

Anthropic by default. With `llm.provider = "openai"`, any server that speaks
the OpenAI chat-completions API: Ollama, LM Studio, llama.cpp, vLLM, LiteLLM,
or OpenAI itself. A local server keeps the whole pipeline on your machines.

    [llm]
    provider = "openai"
    base_url = "http://localhost:11434/v1"   # Ollama's default
    model    = "qwen3:14b"
    api_key  = ""                            # most local servers need none

For Anthropic, `base_url` points the Messages API somewhere else (a proxy
such as LiteLLM, or another server that speaks it), and `model` replaces
the default Haiku.
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from dataclasses import dataclass

from pocket_libre.config import get, resolve_anthropic_key
from pocket_libre.pricing import DEFAULT_MODEL, format_cost

ANTHROPIC = "anthropic"
OPENAI = "openai"
# Names people reach for, all served by the OpenAI-compatible client.
_ALIASES = {
    "claude": ANTHROPIC,
    "openai-compatible": OPENAI,
    "openai_compatible": OPENAI,
    "ollama": OPENAI,
    "lmstudio": OPENAI,
    "lm-studio": OPENAI,
    "llamacpp": OPENAI,
    "llama.cpp": OPENAI,
    "vllm": OPENAI,
    "local": OPENAI,
}
OLLAMA_URL = "http://localhost:11434/v1"


class LLMError(Exception):
    """A model call failed in a way worth showing the user as is."""


@dataclass
class Reply:
    text: str
    input_tokens: int | None = None
    output_tokens: int | None = None


@dataclass
class LLM:
    provider: str = ANTHROPIC
    api_key: str = ""
    model: str = DEFAULT_MODEL
    base_url: str = ""
    # Local models on a laptop can take minutes over a long transcript.
    timeout: float = 600.0

    @property
    def name(self) -> str:
        """For messages: the model, and where it runs if not Anthropic."""
        if self.provider == ANTHROPIC and not self.base_url:
            return self.model
        return f"{self.model} at {self.base_url or OLLAMA_URL}"

    def complete(self, prompt: str, system: str | None = None,
                 max_tokens: int = 1500, model: str | None = None) -> Reply:
        model = model or self.model
        if self.provider == OPENAI:
            return self._openai(prompt, system, max_tokens, model)
        return self._anthropic(prompt, system, max_tokens, model)

    def usage(self, reply: Reply) -> str:
        """Tokens, and the cost where the price is known (Anthropic's Haiku)."""
        if reply.input_tokens is None or reply.output_tokens is None:
            return "token counts not reported"
        if self.provider == ANTHROPIC and self.model == DEFAULT_MODEL:
            return format_cost(reply.input_tokens, reply.output_tokens)
        return f"{reply.input_tokens:,}+{reply.output_tokens:,} tokens"

    # ── Providers ──────────────────────────────

    def _anthropic(self, prompt, system, max_tokens, model) -> Reply:
        try:
            import anthropic
        except ImportError as e:
            raise LLMError("The anthropic package is not installed. "
                           "Run: pip install anthropic") from e
        client = anthropic.Anthropic(api_key=self.api_key or None,
                                     base_url=self.base_url or None)
        kwargs = {"system": system} if system else {}
        message = client.messages.create(
            model=model, max_tokens=max_tokens,
            messages=[{"role": "user", "content": prompt}], **kwargs,
        )
        text = "".join(getattr(b, "text", "") for b in message.content)
        usage = getattr(message, "usage", None)
        return Reply(text, getattr(usage, "input_tokens", None),
                     getattr(usage, "output_tokens", None))

    def _openai(self, prompt, system, max_tokens, model) -> Reply:
        if not model:
            raise LLMError('No model set. Run: pocket-libre config --set llm.model=<name> '
                           '(for Ollama, a name from "ollama list").')
        url = (self.base_url or OLLAMA_URL).rstrip("/") + "/chat/completions"
        messages = ([{"role": "system", "content": system}] if system else []) + [
            {"role": "user", "content": prompt}]
        body = json.dumps({"model": model, "messages": messages,
                           "max_tokens": max_tokens}).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        request = urllib.request.Request(url, data=body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                data = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", errors="replace")[:300].strip()
            raise LLMError(f"{url} answered HTTP {e.code}: {detail or e.reason}") from e
        except urllib.error.URLError as e:
            raise LLMError(f"Can't reach {url}: {e.reason}. Is the server running?") from e
        except (TimeoutError, OSError) as e:
            raise LLMError(f"{url}: {str(e) or type(e).__name__}") from e
        except ValueError as e:
            raise LLMError(f"{url} did not answer with JSON.") from e
        try:
            text = data["choices"][0]["message"]["content"] or ""
        except (KeyError, IndexError, TypeError) as e:
            raise LLMError(f"Unexpected answer from {url}: {str(data)[:300]}") from e
        usage = data.get("usage") or {}
        return Reply(_strip_thinking(text), usage.get("prompt_tokens"),
                     usage.get("completion_tokens"))


def _strip_thinking(text: str) -> str:
    """Reasoning models served locally (Qwen3, DeepSeek-R1) can put their
    reasoning in <think> tags ahead of the answer."""
    return re.sub(r"<think>.*?</think>\s*", "", text, flags=re.DOTALL).strip()


def as_llm(value) -> LLM | None:
    """An LLM from what callers pass around: an LLM, an Anthropic API key
    (what every caller passed before providers existed), or nothing."""
    if value is None or value == "":
        return None
    if isinstance(value, LLM):
        return value
    return LLM(provider=ANTHROPIC, api_key=str(value))


def provider_of(config: dict) -> str:
    raw = str(get(config, "llm", "provider", env_var="POCKET_LIBRE_LLM_PROVIDER",
                  default=ANTHROPIC)).strip().lower()
    return _ALIASES.get(raw, raw)


def resolve_llm(config: dict, anthropic_key: str | None = None) -> LLM | None:
    """The configured model, or None if there isn't one to use.

    `anthropic_key` is a --anthropic-key value; it only applies to Anthropic.
    With an OpenAI-compatible provider, no Anthropic key is ever used, so
    nothing quietly goes to a cloud API.
    """
    provider = provider_of(config)
    model = get(config, "llm", "model", env_var="POCKET_LIBRE_LLM_MODEL", default="")
    base_url = get(config, "llm", "base_url", env_var="POCKET_LIBRE_LLM_BASE_URL",
                   default="")
    if provider == OPENAI:
        if not model:
            return None
        api_key = get(config, "llm", "api_key", env_var="OPENAI_API_KEY", default="")
        return LLM(OPENAI, api_key, model, base_url or OLLAMA_URL)
    if provider != ANTHROPIC:
        return None
    key = resolve_anthropic_key(config, anthropic_key)
    if not key:
        return None
    return LLM(ANTHROPIC, key, model or DEFAULT_MODEL, base_url)


def missing_hint(config: dict) -> str:
    """What to do when resolve_llm() found nothing, for the panel callers show."""
    provider = provider_of(config)
    if provider == OPENAI:
        return ("llm.provider is set to an OpenAI-compatible server, but no model is set.\n"
                "  pocket-libre config --set llm.model=qwen3:14b\n"
                f"  (and llm.base_url if it isn't {OLLAMA_URL})")
    if provider != ANTHROPIC:
        return (f'llm.provider "{provider}" is unknown. '
                'Use "anthropic" or "openai" (any OpenAI-compatible server).')
    return ("No Anthropic API key found. Either:\n"
            "  pocket-libre setup   (or export ANTHROPIC_API_KEY=sk-ant-...)\n"
            "or use a local model with Ollama or any OpenAI-compatible server:\n"
            "  pocket-libre config --set llm.provider=openai\n"
            "  pocket-libre config --set llm.model=qwen3:14b")


def extract_json(text: str):
    """The JSON value in a model's answer, or None.

    Local models are less strict than Claude about answering with JSON only:
    they wrap it in a code fence or a sentence. This takes the first array or
    object that parses.
    """
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = "\n".join(ln for ln in cleaned.split("\n")
                            if not ln.strip().startswith("```"))
    try:
        return json.loads(cleaned)
    except ValueError:
        pass
    decoder = json.JSONDecoder()
    for match in re.finditer(r"[\[{]", cleaned):
        try:
            value, _ = decoder.raw_decode(cleaned[match.start():])
            return value
        except ValueError:
            continue
    return None
