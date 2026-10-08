from datetime import UTC, datetime


def test_activity_tracks_simultaneous_connections_and_completed_durations():
    from app.activity import GatewayActivity

    now = [10.0]
    tracker = GatewayActivity(
        clock=lambda: now[0], utc_clock=lambda: datetime(2026, 10, 8, tzinfo=UTC)
    )
    with tracker.track(7, "HTTP"):
        now[0] = 12
        with tracker.track(9, "SOCKS5"):
            assert [s.proxy_id for s in tracker.active()] == [7, 9]
            assert tracker.active()[0].duration_s == 2
        assert [s.proxy_id for s in tracker.active()] == [7]
        now[0] = 15
    assert tracker.active() == ()
    assert tracker.recent()[0].proxy_id == 7
    assert tracker.recent()[0].duration_s == 5
    for _ in range(25):
        with tracker.track(1, "HTTP"):
            pass
    assert len(tracker.recent()) == 20
