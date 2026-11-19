"""Assign temporary speaker labels to utterances from a mixed call."""
import asyncio
import logging
import threading
from pathlib import Path

import numpy as np

log = logging.getLogger(__name__)


class SpeakerLabels:
    def __init__(self, embed):
        self.embed = embed
        self.centroids = []
        self.lock = asyncio.Lock()

    async def identify(self, pcm: bytes, speech_ms: int) -> int | None:
        if speech_ms < 1200:
            return None
        async with self.lock:
            try:
                vector = await asyncio.to_thread(self.embed, pcm)
            except Exception as exc:
                log.warning("Не удалось определить говорящего (%s)", type(exc).__name__)
                return None
            if vector is None:
                return None
            norm = np.linalg.norm(vector)
            if not np.isfinite(norm) or norm <= 0:
                return None
            vector = vector / norm
            best = sorted(((float(center @ vector), i) for i, center in enumerate(self.centroids)), reverse=True)
            if not best or best[0][0] < 0.45:
                if len(self.centroids) >= 12:
                    return None
                self.centroids.append(vector)
                return len(self.centroids)
            similarity, idx = best[0]
            if similarity < 0.65 or (len(best) > 1 and similarity - best[1][0] < 0.08):
                return None
            updated = 0.8 * self.centroids[idx] + 0.2 * vector
            self.centroids[idx] = updated / np.linalg.norm(updated)
            return idx + 1


def load_embedder(model: str):
    if not model:
        return None
    if not Path(model).is_file():
        log.warning("Файл модели диаризации Телемоста отсутствует")
        return None
    try:
        import sherpa_onnx

        config = sherpa_onnx.SpeakerEmbeddingExtractorConfig(model=model, num_threads=1, provider="cpu")
        if not config.validate():
            return None
        extractor = sherpa_onnx.SpeakerEmbeddingExtractor(config)
    except Exception as exc:
        log.warning("Модель диаризации Телемоста недоступна (%s)", type(exc).__name__)
        return None

    # Serialize inference on the shared native extractor across voice sessions.
    lock = threading.Lock()

    def embed(pcm):
        samples = np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768
        with lock:
            stream = extractor.create_stream()
            stream.accept_waveform(sample_rate=16000, waveform=samples)
            stream.input_finished()
            if not extractor.is_ready(stream):
                return None
            return np.asarray(extractor.compute(stream), dtype=np.float32)

    return embed
