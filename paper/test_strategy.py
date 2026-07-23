from paper.strategy import WalletSignal, bucket_weight, build_signals


def test_weights():
    assert bucket_weight(0.70) == 0.25
    assert bucket_weight(0.60) == 0.0625
    assert bucket_weight(0.50) == 0.03125
    assert bucket_weight(0.49) == 0


def test_cluster_threshold():
    rows = [WalletSignal(str(i), 'mint', 'buy', 0.70, 1_700_000_000 + i, 1.0, 10) for i in range(4)]
    signals = build_signals(rows, now=1_700_000_100, min_strength=1.0)
    assert len(signals) == 1
    assert signals[0].strength == 1.0


def test_old_activity_is_ignored():
    rows = [WalletSignal(str(i), 'mint', 'buy', 0.70, 1_700_000_000, 1.0, 10) for i in range(4)]
    assert build_signals(rows, now=1_700_000_100 + 8 * 86400, min_strength=1.0) == []
