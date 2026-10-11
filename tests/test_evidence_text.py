"""Evidence comparison tolerates typography, not rewritten facts."""

import json

import pytest

from novelagent.memory.evidence_text import excerpt_matches, user_excerpt_matches, event_quote_matches
from novelagent.memory.consolidation import KnowledgePipeline
from novelagent.memory.offline_migration import _quote_matches


PUNCTUATION_PAIRS = [
    ('“村庄”', '"村庄"'), ('‘村庄’', "'村庄'"), ('"村庄"', "'村庄'"),
    ('「村庄」', '"村庄"'), ('『村庄』', "'村庄'"), ('«村庄»', '"村庄"'),
    ('〝村庄〞', '"村庄"'), ('村庄，安全。', '村庄,安全.'),
    ('人物：九叔；地点？村庄！', '人物:九叔;地点?村庄!'), ('人物、地点', '人物,地点'),
    ('（人物）', '(人物)'), ('【人物】', '[人物]'), ('〔人物〕', '[人物]'),
    ('〖人物〗', '[人物]'), ('《村庄》', '<村庄>'), ('〈村庄〉', '<村庄>'),
    ('｛人物｝', '{人物}'), ('等待……然后进入', '等待...然后进入'),
    ('等待⋯然后进入', '等待...然后进入'), ('人物——九叔', '人物--九叔'),
    ('人物–地点', '人物-地点'), ('１＋２＝３', '１+２=３'),
    ('３−２＝１', '３-２=１'), ('范围〜例外', '范围~例外'),
    (r'\"村庄\"', '“村庄”'), (r"\'村庄\'", '"村庄"'),
]
PUNCTUATION_PAIRS += [(chr(code), chr(code - 0xFEE0)) for code in range(0xFF01, 0xFF5F)
                      if not chr(code - 0xFEE0).isalnum()]


@pytest.mark.parametrize('source,excerpt', PUNCTUATION_PAIRS)
def test_punctuation_equivalence_is_shared_and_bidirectional(source, excerpt):
    for original, copied in ((source, excerpt), (excerpt, source)):
        assert excerpt_matches(copied, '之前' + original + '之后')
        assert KnowledgePipeline._quote_matches(copied, {'kind': 'user_input', 'content': original})
        assert _quote_matches(copied, original)
        answer = json.dumps([{'answer': original}], ensure_ascii=False)
        assert user_excerpt_matches(copied, answer)
        assert KnowledgePipeline._quote_matches(copied, {'kind': 'user_answer', 'content': answer})
        assert _quote_matches(copied, answer)


@pytest.mark.parametrize('source,excerpt', [
    ('村庄安全', '村庄危险'), ('人物甲先进入，人物乙随后', '人物乙先进入,人物甲随后'),
    ('最多3件', '最多4件'), ('最多３件', '最多3件'), ('Model A', 'Model a'),
    ('第一条要求，另一条要求，最后要求', '第一条要求,最后要求'),
    ('范围(1,2)', '范围[1,2]'), ('3+2', '3-2'), ('3<2', '3>2'),
    ('3<=2', '3<2'), ('1.2', '1,2'), (r'路径\文件', '路径/文件'),
    ('甲，乙', '甲乙'), ('℃', '°C'), ('①', '1'), ('内容', ''),
])
def test_typography_compatibility_does_not_rewrite_content(source, excerpt):
    assert not excerpt_matches(excerpt, source)


def test_answer_excerpt_cannot_join_values_or_quote_a_json_key():
    content = json.dumps({'question': '甲要求', 'answer': '乙要求'}, ensure_ascii=False)
    assert not user_excerpt_matches('甲要求乙要求', content)
    assert not user_excerpt_matches('answer', content)
    assert user_excerpt_matches('乙要求', content)


@pytest.mark.parametrize('source,quote,accepted', [
    ('abcdefghij', 'abcdefghix', True),  # Exactly 90% matched characters.
    ('abcdefghij', 'abcdefghxy', False),
    ('前文abcdefghij后文', 'abcdefghix', True),
    ('只改 `chapters/` 正文和 `characters/` 人物卡，其余保持原样。',
     '只改 chapters/ 正文和 characters/ 人物卡，其余保持原样。', True),
    ('仪器屏幕只显示重量，不列出物品名称和属性。',
     '屏幕确认所有物品的名称和属性，允许门外依次试错。', False),
    ('任何内容', '', False),
])
def test_event_quotes_use_ninety_percent_continuous_character_similarity(source, quote, accepted):
    assert event_quote_matches(quote, source) is accepted


def test_event_quote_tolerance_does_not_relax_original_or_join_json_values():
    source = '只改 `chapters/` 正文和 `characters/` 人物卡，其余保持原样。'
    quote = source.replace('`', '')
    assert event_quote_matches(quote, json.dumps({'task': source}))
    assert not excerpt_matches(quote, source)
    assert not event_quote_matches('甲要求乙要求', json.dumps({'question': '甲要求', 'answer': '乙要求'}))
    assert not event_quote_matches('answer', json.dumps({'answer': '乙要求'}))
