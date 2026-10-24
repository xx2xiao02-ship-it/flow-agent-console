from flow_agent.credit_pricing import OMNI_COSTS, PRICING, video_estimates


def test_real_credit_conversion_examples_and_unknown_balance():
    count = video_estimates(1050)
    assert count['omni_flash']['8s']['720p'] == 87
    assert count['omni_flash']['8s']['360p'] == 175
    assert count['veo_3_1']['fast'] == {'non_ultra': 52, 'ultra': 105}
    assert count['veo_3_1']['lite'] == {'non_ultra': 105, 'ultra': 210}
    assert count['veo_3_1']['quality'] == {'non_ultra': 10, 'ultra': 10}
    for family in video_estimates(None).values():
        for options in family.values():
            assert all(value is None for value in options.values())
    assert video_estimates(0)['omni_flash']['8s']['720p'] == 0
    assert video_estimates(-1)['omni_flash']['8s']['720p'] == 0


def test_each_wallet_is_rounded_individually():
    a = video_estimates(7)['omni_flash']['8s']['720p']
    b = video_estimates(7)['omni_flash']['8s']['720p']
    assert a + b == 0
    assert video_estimates(14)['omni_flash']['8s']['720p'] == 1


def test_pricing_matches_bundled_engine_and_has_official_source():
    assert OMNI_COSTS['4s'] == {'360p': 4, '720p': 7}
    assert OMNI_COSTS['6s'] == {'360p': 5, '720p': 10}
    assert OMNI_COSTS['10s'] == {'360p': 7, '720p': 15}
    assert PRICING['source_url'].startswith('https://support.google.com/flow/')
