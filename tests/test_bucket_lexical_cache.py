"""Regression tests for BucketManager's lexical scoring cache
(ported from upstream's "Cache recall scoring inputs", 2026-07-17).
"""

import pytest

from bucket_manager import BucketManager


@pytest.mark.asyncio
async def test_bucket_lexical_profile_is_cached_across_calls(test_config):
    mgr = BucketManager(test_config)
    bucket_id = await mgr.create(
        content="小雨和林湛在旧金山讨论了记忆召回的缓存策略。",
        name="记忆召回缓存",
        domain=["技术"],
        tags=["缓存", "召回"],
    )
    bucket = await mgr.get(bucket_id)

    term_frequency_1, weighted_length_1 = mgr._bucket_lexical_profile(bucket)
    assert bucket_id in mgr._lexical_profile_cache
    cached_entry = mgr._lexical_profile_cache[bucket_id]

    term_frequency_2, weighted_length_2 = mgr._bucket_lexical_profile(bucket)
    assert mgr._lexical_profile_cache[bucket_id] is cached_entry
    assert term_frequency_1 == term_frequency_2
    assert weighted_length_1 == weighted_length_2


@pytest.mark.asyncio
async def test_bucket_lexical_profile_recomputes_when_bucket_changes(test_config):
    mgr = BucketManager(test_config)
    bucket_id = await mgr.create(
        content="最初的内容。",
        name="会变化的桶",
        domain=["技术"],
    )
    bucket = await mgr.get(bucket_id)
    mgr._bucket_lexical_profile(bucket)
    stale_entry = mgr._lexical_profile_cache[bucket_id]

    bucket["content"] = "完全不同的新内容，讲的是旧金山的天气。"
    mgr._bucket_lexical_profile(bucket)
    fresh_entry = mgr._lexical_profile_cache[bucket_id]

    assert fresh_entry is not stale_entry
    assert fresh_entry[0] != stale_entry[0]


@pytest.mark.asyncio
async def test_lexical_phrase_boost_matches_name_domain_tags_content(test_config):
    mgr = BucketManager(test_config)
    bucket_id = await mgr.create(
        content="这条记忆的正文提到了旧金山的记忆宫殿。",
        name="旧金山之旅",
        domain=["旅行经历"],
        tags=["旧金山地标"],
    )
    bucket = await mgr.get(bucket_id)

    assert mgr._lexical_phrase_boost(bucket, "旧金山之旅") == 0.95
    assert mgr._lexical_phrase_boost(bucket, "旅行经历") == 0.56
    assert mgr._lexical_phrase_boost(bucket, "旧金山地标") == 0.54
    assert mgr._lexical_phrase_boost(bucket, "记忆宫殿") == 0.48
    assert mgr._lexical_phrase_boost(bucket, "完全不相关的短语") == 0.0
    assert mgr._lexical_phrase_boost(bucket, "短") == 0.0


@pytest.mark.asyncio
async def test_warm_lexical_profiles_prepopulates_cache_for_batch(test_config):
    mgr = BucketManager(test_config)
    bucket_ids = [
        await mgr.create(content=f"内容 {i}", name=f"桶{i}", domain=["技术"])
        for i in range(3)
    ]
    buckets = [await mgr.get(bid) for bid in bucket_ids]

    assert mgr._lexical_profile_cache == {}
    warmed = mgr.warm_lexical_profiles(buckets)
    assert warmed == 3
    for bid in bucket_ids:
        assert bid in mgr._lexical_profile_cache

    # Warming again with unchanged buckets should not recompute (same cached entries).
    cached_entries_before = dict(mgr._lexical_profile_cache)
    mgr.warm_lexical_profiles(buckets)
    for bid in bucket_ids:
        assert mgr._lexical_profile_cache[bid] is cached_entries_before[bid]


def test_warm_lexical_profiles_skips_malformed_entries(test_config):
    mgr = BucketManager(test_config)
    warmed = mgr.warm_lexical_profiles([None, {}, {"id": ""}, "not a dict"])
    assert warmed == 0
    assert mgr._lexical_profile_cache == {}
