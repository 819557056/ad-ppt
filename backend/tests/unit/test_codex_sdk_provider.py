"""Codex SDK provider keeps gateway credentials off the command line."""

from types import SimpleNamespace

import pytest

from services import ai_providers
from services.ai_providers.text import codex_sdk_provider


class FakeThread:
    def __init__(self):
        self.input = None

    def run(self, value):
        self.input = value
        return SimpleNamespace(final_response="answer")

    def turn(self, value):
        self.input = value
        return SimpleNamespace(stream=lambda: iter([
            SimpleNamespace(method="item/started", payload=SimpleNamespace(item=SimpleNamespace(
                type="agentMessage", id="commentary", phase=SimpleNamespace(value="commentary")))),
            SimpleNamespace(method="item/agentMessage/delta", payload=SimpleNamespace(item_id="commentary", delta="skip")),
            SimpleNamespace(method="item/started", payload=SimpleNamespace(item=SimpleNamespace(
                type="agentMessage", id="final", phase=SimpleNamespace(value="final_answer")))),
            SimpleNamespace(method="item/agentMessage/delta", payload=SimpleNamespace(item_id="final", delta="ans")),
            SimpleNamespace(method="item/agentMessage/delta", payload=SimpleNamespace(item_id="final", delta="wer")),
            SimpleNamespace(
                method="turn/completed",
                payload=SimpleNamespace(turn=SimpleNamespace(status=SimpleNamespace(value="completed"))),
            ),
        ]))


class FakeCodex:
    instances = []

    def __init__(self, config):
        self.config = config
        self.thread = FakeThread()
        self.thread_kwargs = None
        self.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def thread_start(self, **kwargs):
        self.thread_kwargs = kwargs
        return self.thread


def test_sdk_uses_app_server_gateway_without_leaking_key(monkeypatch, tmp_path):
    monkeypatch.setattr(codex_sdk_provider, "Codex", FakeCodex)
    monkeypatch.setattr(codex_sdk_provider, "BASE_DIR", str(tmp_path))
    FakeCodex.instances.clear()
    provider = codex_sdk_provider.CodexSDKTextProvider(
        api_key="secret-gateway-key", api_base="https://gateway.example/v1", model="gpt-6-sol"
    )

    assert provider.generate_text("prompt") == "answer"
    assert list(provider.generate_text_stream("prompt")) == ["ans", "wer"]
    image = tmp_path / "image.png"
    image.write_bytes(b"image")
    assert provider.generate_with_image("describe", str(image)) == "answer"

    for instance in FakeCodex.instances:
        config = instance.config
        assert config.env["BANANA_SLIDES_CODEX_GATEWAY_KEY"] == "secret-gateway-key"
        assert config.client_name == "codex_python_sdk"
        assert "secret-gateway-key" not in repr(config.config_overrides)
        assert 'model_providers.banana_slides_gateway.wire_api="responses"' in config.config_overrides
        assert instance.thread_kwargs["model_provider"] == "banana_slides_gateway"
        assert instance.thread_kwargs["ephemeral"] is True


def test_factory_routes_only_text_and_caption_to_codex_sdk(monkeypatch):
    values = {
        "TEXT_MODEL_SOURCE": "codex_sdk",
        "TEXT_API_KEY": "gateway-key",
        "TEXT_API_BASE": "https://gateway.example/v1",
        "IMAGE_CAPTION_MODEL_SOURCE": "codex_sdk",
        "IMAGE_CAPTION_API_KEY": "gateway-key",
        "IMAGE_CAPTION_API_BASE": "https://gateway.example/v1",
        "IMAGE_MODEL_SOURCE": "codex_sdk",
    }
    monkeypatch.setattr(ai_providers, "_resolve_setting", lambda key, fallback=None: values.get(key, fallback))
    assert isinstance(ai_providers.get_text_provider("gpt-6-sol"), codex_sdk_provider.CodexSDKTextProvider)
    assert isinstance(ai_providers.get_caption_provider("gpt-6-sol"), codex_sdk_provider.CodexSDKTextProvider)
    with pytest.raises(ValueError, match="not an image-generation API"):
        ai_providers.get_image_provider("grok-imagine-image")


def test_settings_rejects_sdk_for_image_generation(client):
    response = client.put("/api/settings", json={"image_model_source": "codex_sdk"})
    assert response.status_code == 400
