"""Unit tests for the maths and failure rules the reports depend on."""
import load_tester as lt


def result(status=200, error=None, ms=100.0, page="Home"):
    return lt.RequestResult(user="u", page=page, url="/", status=status, ttfb_ms=ms, total_ms=ms,
                            content_length=1, error=error)


def test_percentile_nearest_rank():
    values = list(range(1, 101))            # 1..100
    assert lt.percentile(values, 95) == 95
    assert lt.percentile(values, 50) == 50


def test_percentile_small_samples_are_not_the_minimum():
    # The old formula returned the minimum for 2 samples; p95 of [10, 900] must be 900.
    assert lt.percentile([10, 900], 95) == 900
    assert lt.percentile([42], 95) == 42
    assert lt.percentile([], 95) is None


def test_4xx_and_5xx_and_transport_errors_are_failures():
    assert not lt.is_failure(result(200))
    assert not lt.is_failure(result(302))
    assert lt.is_failure(result(401))       # expired session must not count as a success
    assert lt.is_failure(result(404))
    assert lt.is_failure(result(503))
    assert lt.is_failure(result(0, error="timeout"))


def test_page_stats_only_time_successful_requests():
    stats = lt.build_page_stats([result(200, ms=100), result(200, ms=300), result(500, ms=5)], {})
    s = stats["Home"].summary()
    assert (s["requests"], s["success"], s["failures"]) == (3, 2, 1)
    assert s["error_rate_%"] == 33.3
    assert s["response_avg_ms"] == 200      # the fast 500 doesn't drag the average down
    assert s["response_p95_ms"] == 300
