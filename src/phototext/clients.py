from __future__ import annotations

from .config import Config
from .mlx_client import MlxClient
from .ollama_client import OllamaClient


def make_client(cfg: Config) -> OllamaClient | MlxClient:
    if cfg.provider == "mlx-serve":
        return MlxClient(cfg)
    return OllamaClient(cfg)
