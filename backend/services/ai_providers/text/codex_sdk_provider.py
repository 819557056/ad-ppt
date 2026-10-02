"""Text/vision provider backed by the official Codex app-server Python SDK.

Unlike the OpenAI-compatible provider, this does not construct HTTP requests to
``/chat/completions``. The SDK launches its pinned Codex runtime and speaks the
app-server protocol over stdio; that runtime sends Responses API traffic to the
configured gateway.
"""

import json
from pathlib import Path
from typing import Generator

from openai_codex import (
    ApprovalMode,
    Codex,
    CodexConfig,
    LocalImageInput,
    Sandbox,
    TextInput,
)

from config import BASE_DIR, Config
from .base import TextProvider, strip_think_tags


_PROVIDER_ID = "banana_slides_gateway"
_GATEWAY_KEY_ENV = "BANANA_SLIDES_CODEX_GATEWAY_KEY"
_INSTRUCTIONS = (
    "You are a content-generation component of Banana Slides. "
    "Do not use tools or inspect local files unless an image was explicitly "
    "attached to the request. Return only the requested content."
)


class CodexSDKTextProvider(TextProvider):
    """Use a real Codex app-server process for text and image-understanding turns."""

    def __init__(self, api_key: str, api_base: str, model: str):
        if not api_key:
            raise ValueError("Codex SDK gateway API key is required")
        if not api_base or not api_base.startswith(("http://", "https://")):
            raise ValueError("Codex SDK gateway Base URL must be an HTTP(S) URL")
        self.api_key = api_key
        self.api_base = api_base.rstrip("/")
        self.model = model
        self.request_timeout_seconds = Config.OPENAI_TIMEOUT
        self.max_attempts = 1

    def _config(self) -> CodexConfig:
        # Keep this integration's sessions/config separate from the user's
        # desktop Codex installation. The gateway key is passed only via env.
        codex_home = Path(BASE_DIR) / "instance" / "codex-sdk"
        codex_home.mkdir(parents=True, exist_ok=True)
        return CodexConfig(
            cwd=str(codex_home),
            env={"CODEX_HOME": str(codex_home), _GATEWAY_KEY_ENV: self.api_key},
            config_overrides=(
                f"model_provider={json.dumps(_PROVIDER_ID)}",
                f"model_providers.{_PROVIDER_ID}.name={json.dumps('Banana Slides Gateway')}",
                f"model_providers.{_PROVIDER_ID}.base_url={json.dumps(self.api_base)}",
                f"model_providers.{_PROVIDER_ID}.wire_api={json.dumps('responses')}",
                f"model_providers.{_PROVIDER_ID}.env_key={json.dumps(_GATEWAY_KEY_ENV)}",
                'web_search="disabled"',
            ),
        )

    def _start_thread(self, codex: Codex):
        return codex.thread_start(
            model=self.model,
            model_provider=_PROVIDER_ID,
            cwd=str(Path(BASE_DIR) / "instance" / "codex-sdk"),
            ephemeral=True,
            sandbox=Sandbox.read_only,
            approval_mode=ApprovalMode.deny_all,
            base_instructions=_INSTRUCTIONS,
        )

    def generate_text(self, prompt: str, thinking_budget: int = 0) -> str:
        with Codex(self._config()) as codex:
            result = self._start_thread(codex).run(prompt)
        answer = strip_think_tags(result.final_response or "")
        if not answer:
            raise RuntimeError("Codex app-server returned no text")
        return answer

    def generate_text_stream(self, prompt: str, thinking_budget: int = 0) -> Generator[str, None, None]:
        with Codex(self._config()) as codex:
            turn = self._start_thread(codex).turn(prompt)
            emitted = False
            final_item_ids = set()
            completed_text = ""
            for event in turn.stream():
                if event.method == "item/started":
                    item = getattr(event.payload.item, "root", event.payload.item)
                    phase = getattr(item, "phase", None)
                    if (
                        getattr(item, "type", None) == "agentMessage"
                        and getattr(phase, "value", phase) != "commentary"
                    ):
                        final_item_ids.add(item.id)
                elif event.method == "item/agentMessage/delta" and event.payload.item_id in final_item_ids:
                    delta = event.payload.delta
                    if delta:
                        emitted = True
                        yield delta
                elif event.method == "item/completed":
                    item = getattr(event.payload.item, "root", event.payload.item)
                    phase = getattr(item, "phase", None)
                    if (
                        getattr(item, "type", None) == "agentMessage"
                        and getattr(phase, "value", phase) != "commentary"
                    ):
                        completed_text = item.text
                elif event.method == "turn/completed":
                    status = event.payload.turn.status.value
                    if status != "completed":
                        error = event.payload.turn.error
                        detail = error.message if error else status
                        raise RuntimeError(f"Codex app-server turn failed: {detail}")
            if not emitted:
                if completed_text:
                    yield completed_text
                else:
                    raise RuntimeError("Codex app-server returned no text")

    def generate_with_image(self, prompt: str, image_path: str, thinking_budget: int = 0) -> str:
        image = Path(image_path).resolve(strict=True)
        with Codex(self._config()) as codex:
            result = self._start_thread(codex).run(
                [TextInput(prompt), LocalImageInput(str(image))]
            )
        answer = strip_think_tags(result.final_response or "")
        if not answer:
            raise RuntimeError("Codex app-server returned no image description")
        return answer
