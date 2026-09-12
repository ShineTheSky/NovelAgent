"""Lazy local SentenceTransformer wrapper."""

import asyncio
from pathlib import Path
from threading import Lock


class LocalEmbeddingModel:
    def __init__(self, model_path: str):
        self.model_path = Path(model_path)
        self._model = None
        self._load_lock = Lock()

    async def encode(self, texts: list[str]):
        return await asyncio.to_thread(self._encode_sync, texts)

    def _encode_sync(self, texts: list[str]):
        return self._get_model().encode(texts, normalize_embeddings=True, convert_to_numpy=True, show_progress_bar=False)

    def _get_model(self):
        if self._model is None:
            with self._load_lock:
                if self._model is None:
                    from sentence_transformers import SentenceTransformer
                    self._model = SentenceTransformer(str(self.model_path), local_files_only=True)
        return self._model
