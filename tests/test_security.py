from conftest import PLAYLIST, api, set_fake_mode, wait_job

from spotifast.security import (
    LoginThrottle,
    generate_temp_password,
    hash_password,
    password_problem,
    verify_password,
)


def test_lockout_is_per_account_and_ip():
    throttle = LoginThrottle(max_failures=5, window_s=900)
    for _ in range(5):
        throttle.record_failure("6.6.6.6", "camille")
    assert throttle.retry_after("6.6.6.6", "camille") > 0
    # a stranger guessing camille's password doesn't lock camille out at home
    assert throttle.retry_after("1.2.3.4", "camille") == 0
    # but the stranger's IP is still blocked for camille
    assert throttle.retry_after("6.6.6.6", "Camille") > 0


def test_ip_guessing_many_accounts_is_blocked():
    throttle = LoginThrottle(max_failures=5, window_s=900)
    for i in range(20):
        throttle.record_failure("6.6.6.6", f"user{i}")
    assert throttle.retry_after("6.6.6.6", "someone-else") > 0
    assert throttle.retry_after("1.2.3.4", "someone-else") == 0


def test_distributed_guessing_on_one_account_is_capped():
    throttle = LoginThrottle(max_failures=5, window_s=900)
    for i in range(50):
        throttle.record_failure(f"10.0.0.{i}", "camille")
    assert throttle.retry_after("1.2.3.4", "camille") > 0


def test_success_resets_only_the_pair():
    throttle = LoginThrottle(max_failures=5, window_s=900)
    for _ in range(4):
        throttle.record_failure("1.2.3.4", "camille")
    throttle.reset("1.2.3.4", "camille")
    for _ in range(4):
        throttle.record_failure("1.2.3.4", "camille")
    assert throttle.retry_after("1.2.3.4", "camille") == 0


def test_password_helpers():
    h = hash_password("une phrase de passe")
    assert h.startswith("$argon2id$")
    assert verify_password(h, "une phrase de passe")
    assert not verify_password(h, "autre chose")
    assert not verify_password(None, "une phrase de passe")
    assert not verify_password("garbage", "x")
    temp = generate_temp_password()
    assert len(temp) == 19 and password_problem(temp, "camille") is None
    assert password_problem("court", "camille")
    assert password_problem("camille-2026-xyz", "camille")
    assert password_problem("aaaaaaaaaaaaaaaa", "bob")


def test_failed_downloads_do_not_eat_the_quota(friend, settings, fake_preview):
    set_fake_mode(settings, "block")
    fake_preview["tracks"] = 10
    for i in range(3):
        r = api(friend, "POST", "/api/jobs",
                json={"url": f"https://open.spotify.com/album/{i}aawyAB9vmqN3uQ7FjRGTy"})
        wait_job(friend, r.json()["job"]["id"])
    assert friend.get("/api/jobs").json()["limits"]["usedToday"] == 0
    assert api(friend, "POST", "/api/jobs", json={"url": PLAYLIST}).status_code == 201
