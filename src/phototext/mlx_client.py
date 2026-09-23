from __future__ import annotations

import json
import time

import requests

from .config import Config
from .imaging import test_image_b64
from .ollama_client import (
    ModelOutputError,
    OllamaServerError,
    OllamaTimeout,
    OllamaUnreachable,
    parse_model_json,
)
from .prompt import (
    EXTRACTION_SCHEMA,
    GATE_PROMPT,
    GATE_SCHEMA,
    PERSON_DESCRIBE_PROMPT,
    PERSON_DESCRIBE_SCHEMA,
    PERSON_MATCH_PROMPT_HEAD,
    PERSON_MATCH_PROMPT_TAIL,
    PERSON_MATCH_SCHEMA,
    SYSTEM_PROMPT,
    USER_PROMPT,
)


def _image_parts(prompt: str, images_b64: list[str]) -> list[dict]:
    parts: list[dict] = [{"type": "text", "text": prompt}]
    for image_b64 in images_b64:
        parts.append(
            {
                "type": "image_url",
                "image_url": {"url": f"data:image/jpeg;base64,{image_b64}"},
            }
        )
    return parts


class MlxClient:
    """mlx-serve (OpenAI-compatible) twin of OllamaClient.

    Same method surface and exception types, so worker/cli/people code is
    provider-agnostic. Raises the Ollama* exceptions from ollama_client on
    purpose: callers catch those.
    """

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.base_url = cfg.mlx_url.rstrip("/")
        self.model = cfg.model
        self.gate_model = cfg.prefilter_model
        self.person_model = cfg.person_model or cfg.prefilter_model
        self.timeout = cfg.request_timeout_s

    def check_connection(self) -> list[str]:
        try:
            resp = requests.get(f"{self.base_url}/v1/models", timeout=5)
            resp.raise_for_status()
        except requests.RequestException as e:
            raise OllamaUnreachable(str(e)) from e
        try:
            data = resp.json().get("data", [])
            return [str(m.get("id") or "") for m in data]
        except ValueError as e:
            raise OllamaServerError(
                f"invalid response from /v1/models: {resp.text[:200]}"
            ) from e

    def loaded_models(self) -> list[str]:
        """Always empty: mlx-serve coexists models under its LRU/budget
        rules, so there is no "pause while a different model is loaded"
        state to respect (unlike single-resident Ollama)."""
        return []

    def unload(self, model: str) -> None:
        """No-op: mlx-serve keeps residency under its own LRU/budget rules."""
        return None

    def preflight(self) -> list[str]:
        try:
            models = self.check_connection()
        except OllamaUnreachable as e:
            return [
                f"cannot reach mlx-serve at {self.base_url} ({e}); is the launchd service running?"
            ]
        if self.model not in models:
            available = ", ".join(sorted(m for m in models if m)) or "none"
            return [
                f"model '{self.model}' is not in the mlx-serve registry (available: {available}); "
                "models are discovered at startup — restart mlx-serve after pulling, "
                "or set 'model' in the config file"
            ]
        try:
            self.extract(test_image_b64())
        except OllamaUnreachable as e:
            return [f"cannot reach mlx-serve at {self.base_url} ({e})"]
        except (OllamaServerError, ModelOutputError) as e:
            return [
                f"model '{self.model}' failed on a test image: {e}; is it a vision model?"
            ]
        return []

    def extract(self, image_b64: str) -> tuple[dict, str]:
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": _image_parts(USER_PROMPT, [image_b64]),
                },
            ],
            "temperature": self.cfg.temperature,
            "max_tokens": self.cfg.max_output_tokens,
        }
        if self.cfg.structured_output:
            payload["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": "extraction",
                    "schema": EXTRACTION_SCHEMA,
                    "strict": False,
                },
            }
        content = self._chat(payload)
        try:
            return parse_model_json(content), content
        except ModelOutputError:
            retry_payload = dict(payload)
            retry_payload["temperature"] = 0.7
            content = self._chat(retry_payload)
            return parse_model_json(content), content

    def gate(self, image_b64: str) -> tuple[dict, str]:
        payload = {
            "model": self.gate_model,
            "messages": [
                {
                    "role": "user",
                    "content": _image_parts(GATE_PROMPT, [image_b64]),
                },
            ],
            "temperature": self.cfg.temperature,
            "max_tokens": 256,
            "response_format": {
                "type": "json_schema",
                "json_schema": {"name": "gate", "schema": GATE_SCHEMA, "strict": False},
            },
        }
        content = self._chat(payload)
        return parse_model_json(content), content

    def describe_person(self, images_b64: list[str]) -> tuple[dict, str]:
        payload = {
            "model": self.person_model,
            "messages": [
                {
                    "role": "user",
                    "content": _image_parts(PERSON_DESCRIBE_PROMPT, images_b64),
                },
            ],
            "temperature": self.cfg.temperature,
            "max_tokens": 700,
        }
        if self.cfg.structured_output:
            payload["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": "person_description",
                    "schema": PERSON_DESCRIBE_SCHEMA,
                    "strict": False,
                },
            }
        content = self._chat(payload)
        return parse_model_json(content), content

    def match_people(self, images_b64: list[str], people_json: str) -> tuple[dict, str]:
        payload = {
            "model": self.person_model,
            "messages": [
                {
                    "role": "user",
                    "content": _image_parts(
                        PERSON_MATCH_PROMPT_HEAD + people_json + PERSON_MATCH_PROMPT_TAIL,
                        images_b64,
                    ),
                },
            ],
            "temperature": self.cfg.temperature,
            "max_tokens": 700,
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": "person_match",
                    "schema": PERSON_MATCH_SCHEMA,
                    "strict": False,
                },
            },
        }
        content = self._chat(payload)
        try:
            return parse_model_json(content), content
        except ModelOutputError:
            retry_payload = dict(payload)
            retry_payload["temperature"] = 0.7
            content = self._chat(retry_payload)
            return parse_model_json(content), content

    def embed(self, texts: list[str]) -> list[list[float]]:
        payload = {"model": self.cfg.embed_model, "input": texts}
        try:
            resp = requests.post(
                f"{self.base_url}/v1/embeddings",
                json=payload,
                timeout=self.timeout,
            )
        except requests.Timeout as e:
            raise OllamaTimeout(f"embed call exceeded {self.timeout}s timeout") from e
        except requests.RequestException as e:
            raise OllamaUnreachable(str(e)) from e
        try:
            resp.raise_for_status()
            data = resp.json()
        except requests.RequestException as e:
            raise OllamaUnreachable(str(e)) from e
        except ValueError as e:
            raise OllamaServerError(
                f"invalid response from /v1/embeddings: {resp.text[:200]}"
            ) from e
        try:
            items = data["data"]
            items = sorted(items, key=lambda d: d.get("index", 0))
            return [item["embedding"] for item in items]
        except (KeyError, TypeError) as e:
            raise ModelOutputError(
                f"unexpected /v1/embeddings shape: {resp.text[:200]}"
            ) from e

    def _chat(self, payload: dict) -> str:
        # Streamed and wall-clock capped for the same reasons as
        # OllamaClient._chat: a server that trickles bytes can keep a call
        # open forever past per-read socket timeouts.
        try:
            started = time.monotonic()
            resp = requests.post(
                f"{self.base_url}/v1/chat/completions",
                json=payload,
                timeout=self.timeout,
                stream=True,
            )
        except requests.Timeout as e:
            raise OllamaTimeout(f"model call exceeded the {self.timeout}s timeout") from e
        except requests.RequestException as e:
            raise OllamaUnreachable(str(e)) from e
        try:
            if resp.status_code != 200:
                raise OllamaServerError(
                    f"HTTP {resp.status_code}: {resp.text[:200]}"
                )
            parts: list[bytes] = []
            try:
                for chunk in resp.iter_content(chunk_size=1):
                    if time.monotonic() - started > self.timeout:
                        raise OllamaTimeout(
                            f"model call exceeded the {self.timeout}s timeout"
                        )
                    parts.append(chunk)
            except requests.Timeout as e:
                raise OllamaTimeout(
                    f"model call exceeded the {self.timeout}s timeout"
                ) from e
            except requests.RequestException as e:
                raise OllamaUnreachable(str(e)) from e
            body = b"".join(parts).decode("utf-8", "replace")
        finally:
            resp.close()
        try:
            return json.loads(body)["choices"][0]["message"]["content"]
        except (ValueError, KeyError, TypeError, IndexError) as e:
            raise ModelOutputError(f"unexpected response shape: {body[:200]}") from e
