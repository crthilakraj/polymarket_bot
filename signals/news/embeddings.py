"""Local embedding similarity pre-filter: a cheap way to decide which markets a
headline is plausibly relevant to, without spending an API call per headline.
"""

import math
from collections.abc import Sequence
from typing import Protocol

DEFAULT_MODEL_NAME = "BAAI/bge-small-en-v1.5"


class Embedder(Protocol):
    def embed(self, text: str) -> Sequence[float]:
        """Return a dense embedding vector for text."""
        ...


class FastEmbedEmbedder:
    """Local embedding model via fastembed (ONNX runtime - no torch/GPU needed).

    The model is downloaded from Hugging Face on first use (~130MB for the
    default `bge-small-en-v1.5`) and cached locally afterwards, so there's no
    per-call network round trip - that's the point: this replaces an API call
    per headline with a local forward pass.
    """

    def __init__(self, model_name: str = DEFAULT_MODEL_NAME):
        from fastembed import TextEmbedding

        self._model = TextEmbedding(model_name=model_name)

    def embed(self, text: str) -> Sequence[float]:
        return next(iter(self._model.embed([text]))).tolist()


def cosine_similarity(a: Sequence[float], b: Sequence[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return dot / (norm_a * norm_b)
