from __future__ import annotations

import json
import re
import time

import requests

from .config import Config
from .imaging import test_image_b64
from .prompt import (
    EXTRACTION_SCHEMA,
    GATE_SCHEMA,
    GATE_PROMPT,
    PERSON_DESCRIBE_PROMPT,
    PERSON_DESCRIBE_SCHEMA,
    PERSON_MATCH_PROMPT_HEAD,
    PERSON_MATCH_PROMPT_TAIL,
    PERSON_MATCH_SCHEMA,
    SYSTEM_PROMPT,
    USER_PROMPT,
)


class OllamaUnreachable(Exception):
    pass


class OllamaTimeout(Exception):
    pass


class OllamaServerError(Exception):
    pass


class ModelOutputError(Exception):
    def __init__(self, message: str, content: str | None = None):
        super().__init__(message)
        self.content = content


_SURROGATE_RE = re.compile("[\ud800-\udfff]")


def _clean_surrogates(value):
    """Replace unpaired surrogates in model output with U+FFFD.

    Models sometimes emit an unpaired surrogate escape (half of an emoji).
    json.loads accepts it, but the resulting str cannot be stored in SQLite
    (strict UTF-8) and would kill the run, so scrub every string here —
    the single parse choke point for all model calls. Properly paired
    escapes are combined into the real character by json.loads itself and
    never match.
    """
    if isinstance(value, str):
        return _SURROGATE_RE.sub("\ufffd", value)
    if isinstance(value, list):
        return [_clean_surrogates(item) for item in value]
    if isinstance(value, dict):
        return {key: _clean_surrogates(item) for key, item in value.items()}
    return value


def parse_model_json(content: str | None) -> dict:
    if content:
        try:
            value = json.loads(content)
            if isinstance(value, dict):
                return _clean_surrogates(value)
        except ValueError:
            pass
        start, end = content.find("{"), content.rfind("}")
        if start != -1 and end > start:
            try:
                value = json.loads(content[start : end + 1])
                if isinstance(value, dict):
                    return _clean_surrogates(value)
            except ValueError:
                pass
    raise ModelOutputError("model did not return usable JSON", content)


class OllamaClient:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.base_url = cfg.ollama_url.rstrip("/")
        self.model = cfg.model
        self.gate_model = cfg.prefilter_model
        self.person_model = cfg.person_model or cfg.prefilter_model
        self.timeout = cfg.request_timeout_s

    def check_connection(self) -> list[str]:
        try:
            resp = requests.get(f"{self.base_url}/api/tags", timeout=5)
            resp.raise_for_status()
        except requests.RequestException as e:
            raise OllamaUnreachable(str(e)) from e
        try:
            return [m.get("name") or m.get("model") or "" for m in resp.json().get("models", [])]
        except ValueError as e:
            raise OllamaServerError(f"invalid response from /api/tags: {resp.text[:200]}") from e

    def loaded_models(self) -> list[str]:
        """Model names currently loaded in Ollama's memory (/api/ps)."""
        try:
            resp = requests.get(f"{self.base_url}/api/ps", timeout=5)
            resp.raise_for_status()
        except requests.RequestException as e:
            raise OllamaUnreachable(str(e)) from e
        try:
            return [m.get("name") or m.get("model") or "" for m in resp.json().get("models", [])]
        except ValueError as e:
            raise OllamaServerError(f"invalid response from /api/ps: {resp.text[:200]}") from e

    def preflight(self) -> list[str]:
        try:
            models = self.check_connection()
        except OllamaUnreachable as e:
            return [f"cannot reach Ollama at {self.base_url} ({e}); is `ollama serve` running?"]
        if self.model not in models:
            available = ", ".join(sorted(m for m in models if m)) or "none"
            return [
                f"model '{self.model}' is not installed in Ollama (available: {available}); "
                f"run `ollama pull {self.model}` or set 'model' in the config file"
            ]
        try:
            self.extract(test_image_b64())
        except OllamaUnreachable as e:
            return [f"cannot reach Ollama at {self.base_url} ({e})"]
        except (OllamaServerError, ModelOutputError) as e:
            return [f"model '{self.model}' failed on a test image: {e}; is it a vision model?"]
        return []

    def extract(self, image_b64: str) -> tuple[dict, str]:
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": USER_PROMPT, "images": [image_b64]},
            ],
            "stream": False,
            "format": EXTRACTION_SCHEMA if self.cfg.structured_output else "json",
            "think": False,
            "options": {
                "temperature": self.cfg.temperature,
                "num_ctx": self.cfg.num_ctx,
                "num_predict": self.cfg.max_output_tokens,
            },
        }
        content = self._chat(payload)
        try:
            return parse_model_json(content), content
        except ModelOutputError:
            # Greedy decoding (temperature 0) can repetition-loop until the
            # token cap truncates the JSON mid-string. One retry with higher
            # temperature plus a repetition penalty escapes the loop
            # (measured: repeat_penalty alone does not).
            retry_payload = dict(payload)
            retry_payload["options"] = {
                **payload["options"],
                "temperature": 0.7,
                "repeat_penalty": 1.2,
            }
            content = self._chat(retry_payload)
            return parse_model_json(content), content

    def gate(self, image_b64: str) -> tuple[dict, str]:
        """Cheap pre-filter: does this photo contain visible text?

        Uses the small `prefilter_model` with a short prompt and a tiny
        output budget. Raises the same exceptions as extract(); the caller
        treats failures as "just do the full pass".
        """
        payload = {
            "model": self.gate_model,
            "messages": [
                {"role": "user", "content": GATE_PROMPT, "images": [image_b64]},
            ],
            "stream": False,
            "format": GATE_SCHEMA,
            "think": False,
            "options": {
                "temperature": self.cfg.temperature,
                "num_ctx": self.cfg.num_ctx,
                "num_predict": 256,
            },
        }
        content = self._chat(payload)
        return parse_model_json(content), content

    def describe_person(self, images_b64: list[str]) -> tuple[dict, str]:
        """Build a recognition profile for one person from seed crops."""
        payload = {
            "model": self.person_model,
            "messages": [
                {
                    "role": "user",
                    "content": PERSON_DESCRIBE_PROMPT,
                    "images": list(images_b64),
                },
            ],
            "stream": False,
            "format": PERSON_DESCRIBE_SCHEMA if self.cfg.structured_output else "json",
            "think": False,
            "options": {
                "temperature": self.cfg.temperature,
                "num_ctx": self.cfg.num_ctx,
                "num_predict": 700,
            },
        }
        content = self._chat(payload)
        return parse_model_json(content), content

    def match_people(self, images_b64: list[str], people_json: str) -> tuple[dict, str]:
        """Check which registered people appear in one photo.

        One call evaluates every person (the prompt embeds their profiles).
        `images_b64` is one whole photo or a few face crops. Uses the same
        anti-loop retry as extract().
        """
        payload = {
            "model": self.person_model,
            "messages": [
                {
                    "role": "user",
                    "content": PERSON_MATCH_PROMPT_HEAD + people_json + PERSON_MATCH_PROMPT_TAIL,
                    "images": list(images_b64),
                },
            ],
            "stream": False,
            "format": PERSON_MATCH_SCHEMA if self.cfg.structured_output else "json",
            "think": False,
            "options": {
                "temperature": self.cfg.temperature,
                "num_ctx": self.cfg.num_ctx,
                "num_predict": 700,
            },
        }
        content = self._chat(payload)
        try:
            return parse_model_json(content), content
        except ModelOutputError:
            retry_payload = dict(payload)
            retry_payload["options"] = {
                **payload["options"],
                "temperature": 0.7,
                "repeat_penalty": 1.2,
            }
            content = self._chat(retry_payload)
            return parse_model_json(content), content

    def unload(self, model: str) -> None:
        """Ask Ollama to drop a model from memory (best effort)."""
        try:
            requests.post(
                f"{self.base_url}/api/generate",
                json={"model": model, "keep_alive": 0},
                timeout=self.timeout,
            )
        except requests.RequestException:
            pass

    def embed(self, texts: list[str]) -> list[list[float]]:
        payload = {"model": self.cfg.embed_model, "input": texts}
        try:
            resp = requests.post(
                f"{self.base_url}/api/embed",
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
            raise OllamaServerError(f"invalid response from /api/embed: {resp.text[:200]}") from e
        try:
            return data["embeddings"]
        except (KeyError, TypeError) as e:
            raise ModelOutputError(f"unexpected /api/embed shape: {resp.text[:200]}") from e

    def _chat(self, payload: dict) -> str:
        # Streamed and wall-clock capped: requests' timeout is a per-read
        # socket timeout, so a server that trickles bytes can keep a call
        # open forever even with request_timeout_s set. The budget below
        # bounds the whole call — headers, body, and the 400-think retry.
        payload.setdefault("keep_alive", "30m")
        try:
            started = time.monotonic()
            resp = requests.post(
                f"{self.base_url}/api/chat",
                json=payload,
                timeout=self.timeout,
                stream=True,
            )
        except requests.Timeout as e:
            raise OllamaTimeout(f"model call exceeded the {self.timeout}s timeout") from e
        except requests.RequestException as e:
            raise OllamaUnreachable(str(e)) from e
        try:
            if resp.status_code == 400 and "think" in resp.text.lower():
                payload.pop("think", None)
                resp.close()
                try:
                    resp = requests.post(
                        f"{self.base_url}/api/chat",
                        json=payload,
                        timeout=self.timeout,
                        stream=True,
                    )
                except requests.Timeout as e:
                    raise OllamaTimeout(
                        f"model call exceeded the {self.timeout}s timeout"
                    ) from e
                except requests.RequestException as e:
                    raise OllamaUnreachable(str(e)) from e
            if resp.status_code != 200:
                raise OllamaServerError(f"HTTP {resp.status_code}: {resp.text[:200]}")
            parts: list[bytes] = []
            try:
                # chunk_size=1 matters: larger reads block until the full
                # chunk arrives (or EOF), which would postpone the deadline
                # check past the budget. Responses are small (KBs), so the
                # per-byte overhead is negligible.
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
            return json.loads(body)["message"]["content"]
        except (ValueError, KeyError, TypeError) as e:
            raise ModelOutputError(f"unexpected response shape: {body[:200]}") from e
