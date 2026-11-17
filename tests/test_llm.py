"""The language model: Anthropic by default, or any OpenAI-compatible server (#39)."""

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from pocket_libre import llm as llm_mod
from pocket_libre.analyze import analyze_transcript, chat_with_recording
from pocket_libre.diarize import diarize_auto
from pocket_libre.llm import LLM, LLMError, Reply, as_llm, extract_json, resolve_llm
from pocket_libre.pricing import DEFAULT_MODEL
from pocket_libre.summarize import summarize_transcript


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for var in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "POCKET_LIBRE_LLM_PROVIDER",
                "POCKET_LIBRE_LLM_MODEL", "POCKET_LIBRE_LLM_BASE_URL"):
        monkeypatch.delenv(var, raising=False)


# ── Choosing the model ──────────────────────────


def test_anthropic_is_the_default():
    model = resolve_llm({"api": {"anthropic_key": "sk-ant-x"}})
    assert (model.provider, model.api_key, model.model) == ("anthropic", "sk-ant-x", DEFAULT_MODEL)


def test_nothing_configured_means_no_model():
    assert resolve_llm({}) is None


def test_anthropic_model_and_base_url_can_be_changed():
    model = resolve_llm({"api": {"anthropic_key": "k"},
                         "llm": {"model": "claude-sonnet-4-5", "base_url": "http://proxy:4000"}})
    assert (model.model, model.base_url) == ("claude-sonnet-4-5", "http://proxy:4000")


@pytest.mark.parametrize("name", ["openai", "openai-compatible", "Ollama", "lmstudio", "vllm"])
def test_openai_compatible_names(name):
    model = resolve_llm({"llm": {"provider": name, "model": "qwen3:14b"}})
    assert model.provider == "openai"
    assert model.base_url == "http://localhost:11434/v1"


def test_a_local_provider_never_uses_the_anthropic_key(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-env")
    config = {"api": {"anthropic_key": "sk-ant-file"}, "llm": {"provider": "openai"}}
    # No model: nothing to use, and no quiet fallback to the cloud.
    assert resolve_llm(config, anthropic_key="sk-ant-cli") is None
    config["llm"]["model"] = "llama3.2"
    assert resolve_llm(config, anthropic_key="sk-ant-cli").api_key == ""


def test_unknown_provider_means_no_model():
    assert resolve_llm({"api": {"anthropic_key": "k"}, "llm": {"provider": "gemini"}}) is None
    assert "unknown" in llm_mod.missing_hint({"llm": {"provider": "gemini"}})


def test_environment_overrides(monkeypatch):
    monkeypatch.setenv("POCKET_LIBRE_LLM_PROVIDER", "ollama")
    monkeypatch.setenv("POCKET_LIBRE_LLM_MODEL", "mistral")
    monkeypatch.setenv("POCKET_LIBRE_LLM_BASE_URL", "http://gpu-box:8000/v1")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-local")
    model = resolve_llm({})
    assert (model.provider, model.model, model.base_url, model.api_key) == (
        "openai", "mistral", "http://gpu-box:8000/v1", "sk-local")


def test_an_api_key_string_still_means_anthropic():
    assert as_llm("sk-ant-x") == LLM("anthropic", "sk-ant-x")
    assert as_llm(None) is None
    model = LLM("openai", model="m")
    assert as_llm(model) is model


# ── The OpenAI-compatible client, against a real HTTP server ──


class _Server:
    def __init__(self, status=200, answer=None):
        self.requests = []
        server = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                server.requests.append((self.path, dict(self.headers), body))
                payload = json.dumps(answer if answer is not None else {
                    "choices": [{"message": {"content": "<think>hmm</think>\nHello"}}],
                    "usage": {"prompt_tokens": 12, "completion_tokens": 3},
                }).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *args):
                pass

        self.httpd = HTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_port}/v1"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


@pytest.fixture
def server():
    started = []

    def start(**kwargs):
        s = _Server(**kwargs)
        started.append(s)
        return s

    yield start
    for s in started:
        s.close()


def test_openai_compatible_request_and_reply(server):
    srv = server()
    model = LLM("openai", "sk-local", "qwen3:14b", srv.url + "/")
    reply = model.complete("the prompt", system="be brief", max_tokens=99)

    assert reply == Reply("Hello", 12, 3)     # reasoning stripped
    path, headers, body = srv.requests[0]
    assert path == "/v1/chat/completions"
    assert headers["Authorization"] == "Bearer sk-local"
    assert body == {"model": "qwen3:14b", "max_tokens": 99, "messages": [
        {"role": "system", "content": "be brief"},
        {"role": "user", "content": "the prompt"}]}
    assert model.usage(reply) == "12+3 tokens"


def test_no_api_key_sends_no_authorization(server):
    srv = server()
    LLM("openai", "", "m", srv.url).complete("p")
    assert "Authorization" not in srv.requests[0][1]


def test_http_error_is_explained(server):
    srv = server(status=404, answer={"error": "model 'nope' not found"})
    with pytest.raises(LLMError, match="HTTP 404.*not found"):
        LLM("openai", "", "nope", srv.url).complete("p")


def test_unreachable_server_is_explained():
    with pytest.raises(LLMError, match="Can't reach .*Is the server running"):
        LLM("openai", "", "m", "http://127.0.0.1:9/v1", timeout=2).complete("p")


def test_a_missing_model_is_explained():
    with pytest.raises(LLMError, match="No model set"):
        LLM("openai", "", "", "http://127.0.0.1:9/v1").complete("p")


def test_unexpected_answer_is_explained(server):
    srv = server(answer={"nope": True})
    with pytest.raises(LLMError, match="Unexpected answer"):
        LLM("openai", "", "m", srv.url).complete("p")


def test_cost_only_where_the_price_is_known():
    assert "$" in LLM().usage(Reply("x", 1000, 100))
    assert "$" not in LLM(model="claude-opus-4-5").usage(Reply("x", 1000, 100))
    assert LLM("openai", model="m").usage(Reply("x")) == "token counts not reported"


# ── JSON from chatty models ─────────────────────


@pytest.mark.parametrize("text, value", [
    ('{"a": 1}', {"a": 1}),
    ('```json\n[1, 2]\n```', [1, 2]),
    ('Sure! Here is the JSON:\n[{"line": 0, "speaker": "Host"}]\nHope that helps.',
     [{"line": 0, "speaker": "Host"}]),
    ("No JSON here.", None),
])
def test_extract_json(text, value):
    assert extract_json(text) == value


# ── Every feature goes through the configured model ──


class _Canned(LLM):
    """Answers every prompt with `text` and records what it was asked."""

    def __init__(self, text):
        super().__init__("openai", model="local-model", base_url="http://x/v1")
        self.text = text
        self.prompts = []

    def complete(self, prompt, system=None, max_tokens=1500, model=None):
        self.prompts.append((system, prompt))
        return Reply(self.text, 10, 5)


def test_summary_uses_the_configured_model():
    model = _Canned("## Summary\nIt went fine.")
    assert summarize_transcript("[00:01] A: hi", model) == "## Summary\nIt went fine."
    assert "[00:01] A: hi" in model.prompts[0][1]


def test_analysis_reads_json_from_a_chatty_answer():
    model = _Canned('Here you go:\n{"people": ["Ana"], "topics": []}')
    assert analyze_transcript("t", model, "entities") == {"people": ["Ana"], "topics": []}


def test_analysis_keeps_an_answer_that_isnt_json():
    assert analyze_transcript("t", _Canned("I can't do that."), "entities") == {
        "_raw": "I can't do that."}


def test_chat_uses_the_configured_model():
    model = _Canned("At [00:01].")
    assert chat_with_recording("transcript", "when?", model) == "At [00:01]."
    assert "Question: when?" in model.prompts[0][1]


SEGMENTS = [{"start": 0.0, "end": 2.0, "text": "Welcome to the show."},
            {"start": 2.0, "end": 4.0, "text": "Thanks for having me."}]


def test_speaker_labels_use_the_configured_model():
    model = _Canned('Speakers:\n[{"line": 0, "speaker": "Host"}, {"line": 1, "speaker": "Guest"}]')
    speakers = diarize_auto(SEGMENTS, anthropic_key=model)
    assert [s.speaker for s in speakers] == ["Host", "Guest"]


def test_speaker_labels_fall_back_when_the_answer_is_unusable():
    speakers = diarize_auto(SEGMENTS, anthropic_key=_Canned("Two people talk."))
    assert [s.speaker for s in speakers] == ["Speaker", "Speaker"]


def test_anthropic_client_gets_the_key_base_url_and_system(monkeypatch):
    import sys
    import types

    calls = {}

    class _Messages:
        def create(self, **kwargs):
            calls["create"] = kwargs
            return types.SimpleNamespace(
                content=[types.SimpleNamespace(text="ok")],
                usage=types.SimpleNamespace(input_tokens=3, output_tokens=1))

    class _Anthropic:
        def __init__(self, api_key=None, base_url=None):
            calls["client"] = (api_key, base_url)
            self.messages = _Messages()

    monkeypatch.setitem(sys.modules, "anthropic", types.SimpleNamespace(Anthropic=_Anthropic))
    reply = LLM("anthropic", "sk-ant-x", base_url="http://proxy:4000").complete("p", max_tokens=7)
    assert reply == Reply("ok", 3, 1)
    assert calls["client"] == ("sk-ant-x", "http://proxy:4000")
    assert calls["create"] == {"model": DEFAULT_MODEL, "max_tokens": 7,
                               "messages": [{"role": "user", "content": "p"}]}
