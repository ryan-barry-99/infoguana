import logging
import threading
from typing import Optional

from app.config import settings


log = logging.getLogger(__name__)


class EmbeddingEngine:
    """Lazy singleton around fastembed. Thread-safe."""

    def __init__(self, model_name: str) -> None:
        self.model_name = model_name
        self._model = None
        self._lock = threading.Lock()

    def _load(self) -> None:
        if self._model is not None:
            return
        with self._lock:
            if self._model is not None:
                return
            from fastembed import TextEmbedding
            log.info("loading embedding model %s", self.model_name)
            self._model = TextEmbedding(model_name=self.model_name)
            log.info("embedding model ready")

    def embed(self, text: str) -> list[float]:
        self._load()
        assert self._model is not None
        vecs = list(self._model.embed([text]))
        return vecs[0].tolist()

    def embed_many(self, texts: list[str]) -> list[list[float]]:
        self._load()
        assert self._model is not None
        return [v.tolist() for v in self._model.embed(texts)]

    def warm(self) -> None:
        """Fire-and-forget warmup — load model + embed a dummy string."""
        try:
            self._load()
            _ = self.embed("warmup")
        except Exception:
            log.exception("embedding warmup failed")


_engine: Optional[EmbeddingEngine] = None


def engine() -> EmbeddingEngine:
    global _engine
    if _engine is None:
        _engine = EmbeddingEngine(settings.embed_model)
    return _engine
