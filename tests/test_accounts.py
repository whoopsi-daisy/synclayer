import time

from jsm.providers.accounts import WINDOW_SECONDS, AccountManager


def test_pick_best_prefers_most_remaining(db):
    mgr = AccountManager(db, [("a", "pa"), ("b", "pb")])
    now = time.time()
    for _ in range(5):
        mgr.record_download("a", now)
    assert mgr.pick_best(now) == "b"
    assert mgr.quota("a", now).remaining == 15
    assert mgr.quota("b", now).remaining == 20


def test_quota_window_rolls(db):
    mgr = AccountManager(db, [("a", "pa")], daily_limit=2)
    now = time.time()
    mgr.record_download("a", now - WINDOW_SECONDS - 10)  # outside window
    mgr.record_download("a", now - 100)
    assert mgr.quota("a", now).used == 1
    assert mgr.pick_best(now) == "a"


def test_exhaustion_and_next_available(db):
    mgr = AccountManager(db, [("a", "pa"), ("b", "pb")], daily_limit=1)
    now = time.time()
    mgr.record_download("a", now - 3600)
    mgr.record_download("b", now - 60)
    assert mgr.pick_best(now) is None
    # account a's download is older, so it frees up first
    expected = (now - 3600) + WINDOW_SECONDS
    assert abs(mgr.next_available_time(now) - expected) < 1


def test_next_available_none_when_quota_free(db):
    mgr = AccountManager(db, [("a", "pa")])
    assert mgr.next_available_time() is None
    assert AccountManager(db, []).next_available_time() is None


def test_password_lookup(db):
    mgr = AccountManager(db, [("a", "secret")])
    assert mgr.password_for("a") == "secret"
    assert mgr.password_for("nope") is None
