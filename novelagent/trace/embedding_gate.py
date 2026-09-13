"""Conservative local embedding gate for routine user requests."""

from dataclasses import dataclass

from novelagent.embeddings.local_model import LocalEmbeddingModel


_ROUTINE_PROTOTYPES = (
    "继续写作", "继续下一节", "扩写已有正文", "读取文件", "列出目录", "执行工具操作",
)
_FEEDBACK_PROTOTYPES = (
    "这里有错误，需要修改", "不要这样写", "应该调整这个问题", "这个很好，请保持", "我对结果有反馈",
)
_FEEDBACK_MARKERS = ("错误", "有问题", "不对", "修改", "改成", "不要", "别再", "应该", "建议", "不错", "很好", "满意", "保持")


@dataclass(frozen=True)
class GateResult:
    skip: bool
    routine_similarity: float = 0.0
    feedback_similarity: float = 0.0


class EmbeddingGate:
    """Fail open: unavailable embeddings always leave Trace analysis enabled."""

    def __init__(self, model_path: str, enabled: bool = True, routine_min_similarity: float = 0.70,
                 routine_min_margin: float = 0.12):
        self.enabled = enabled
        self.routine_min_similarity = routine_min_similarity
        self.routine_min_margin = routine_min_margin
        self.embedding_model = LocalEmbeddingModel(model_path)

    async def evaluate(self, user_inputs: list[str]) -> GateResult:
        texts = [text.strip() for text in user_inputs if text and text.strip()]
        if not self.enabled or not texts or any(marker in text for text in texts for marker in _FEEDBACK_MARKERS):
            return GateResult(skip=False)
        try:
            return await self._evaluate_sync(texts)
        except Exception as exc:
            print(f"[embedding_gate] unavailable; keep trace analysis enabled: {exc}", flush=True)
            return GateResult(skip=False)

    async def _evaluate_sync(self, user_inputs: list[str]) -> GateResult:
        vectors = await self.embedding_model.encode(
            [*user_inputs, *_ROUTINE_PROTOTYPES, *_FEEDBACK_PROTOTYPES],
        )
        user_vectors = vectors[:len(user_inputs)]
        routine_vectors = vectors[len(user_inputs):len(user_inputs) + len(_ROUTINE_PROTOTYPES)]
        feedback_vectors = vectors[-len(_FEEDBACK_PROTOTYPES):]
        scores = [
            (float((vector @ routine_vectors.T).max()), float((vector @ feedback_vectors.T).max()))
            for vector in user_vectors
        ]
        routine = min(score[0] for score in scores)
        feedback = max(score[1] for score in scores)
        return GateResult(
            skip=all(routine_score >= self.routine_min_similarity and routine_score - feedback_score >= self.routine_min_margin
                     for routine_score, feedback_score in scores),
            routine_similarity=routine,
            feedback_similarity=feedback,
        )
