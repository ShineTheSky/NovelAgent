from novelagent.rag.store import reciprocal_rank_fusion


def test_rrf_rewards_results_supported_by_both_retrievers():
    scores = reciprocal_rank_fusion(["lexical", "shared"], ["semantic", "shared"])

    assert scores["shared"] > scores["lexical"]
    assert scores["shared"] > scores["semantic"]


def test_rrf_uses_rank_instead_of_source_score_scale():
    scores = reciprocal_rank_fusion(["first", "second"])

    assert scores["first"] == 1 / 61
    assert scores["second"] == 1 / 62


def test_rrf_keeps_single_retriever_fallback():
    scores = reciprocal_rank_fusion(["first", "second"], [])

    assert list(sorted(scores, key=scores.get, reverse=True)) == ["first", "second"]
