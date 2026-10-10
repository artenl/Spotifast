from conftest import ORIGIN, PASSWORD, api, csrf_of, login, make_user


def test_everything_requires_login(client):
    r = client.get("/")
    assert r.status_code == 303 and r.headers["location"] == "/login?next=/"
    assert client.get("/admin").status_code == 303
    assert client.get("/api/jobs").status_code == 401
    assert client.get("/login").status_code == 200
    assert client.get("/healthz").json() == {"status": "ok"}


def test_login_sets_hardened_cookie(app, client):
    make_user(app, "camille")
    r = login(client, "camille")
    assert r.status_code == 303 and r.headers["location"] == "/"
    cookie = r.headers["set-cookie"]
    assert cookie.startswith("__Host-spotifast=")
    for attr in ("HttpOnly", "Secure", "Path=/", "SameSite=lax"):
        assert attr.lower() in cookie.lower()
    assert "domain" not in cookie.lower()
    assert client.get("/").status_code == 200


def test_wrong_password_and_unknown_user_look_the_same(app, client):
    make_user(app, "camille")
    bad_pw = login(client, "camille", "nope nope nope")
    no_user = login(client, "personne", "nope nope nope")
    assert bad_pw.status_code == no_user.status_code == 401
    assert "Identifiant ou mot de passe incorrect" in bad_pw.text
    assert "Identifiant ou mot de passe incorrect" in no_user.text
    assert "set-cookie" not in bad_pw.headers


def test_login_is_throttled_after_repeated_failures(app, client):
    make_user(app, "camille")
    for _ in range(5):
        assert login(client, "camille", "wrong password!").status_code == 401
    # even the right password is refused while locked out
    r = login(client, "camille")
    assert r.status_code == 429 and "Trop de tentatives" in r.text


def test_disabled_user_cannot_log_in(app, client):
    user = make_user(app, "camille")
    app.state.db.set_active(user.id, False)
    assert login(client, "camille").status_code == 401


def test_open_redirect_is_blocked(app, client):
    make_user(app, "camille")
    for target in ("//evil.example/x", "https://evil.example", "/\\evil.example", "javascript:x"):
        client.cookies.clear()
        r = client.post("/login", data={"username": "camille", "password": PASSWORD,
                                        "next": target})
        assert r.headers["location"] == "/", target


def test_forced_password_change_on_first_login(app, client):
    make_user(app, "camille", must_change=True)
    r = login(client, "camille")
    assert r.headers["location"] == "/account"
    assert client.get("/").headers["location"] == "/account"
    assert client.get("/api/jobs").status_code == 403

    csrf = csrf_of(client)
    weak = client.post("/account/password", data={"csrf": csrf, "current": PASSWORD,
                                                   "new": "short", "confirm": "short"})
    assert weak.status_code == 400
    new = "une phrase de passe solide"
    ok = client.post("/account/password", data={"csrf": csrf, "current": PASSWORD,
                                                 "new": new, "confirm": new})
    assert ok.status_code == 303 and ok.headers["location"] == "/account?changed=1"
    assert "Mot de passe changé" in client.get("/account?changed=1").text
    assert client.get("/").status_code == 200

    client.cookies.clear()
    assert login(client, "camille").status_code == 401
    assert login(client, "camille", new).status_code == 303


def test_password_change_logs_out_other_devices(app, client):
    make_user(app, "camille")
    login(client, "camille")
    other_device = client.cookies.get("__Host-spotifast")
    client.cookies.clear()
    login(client, "camille")
    new = "encore une autre phrase"
    client.post("/account/password", data={"csrf": csrf_of(client), "current": PASSWORD,
                                           "new": new, "confirm": new})
    assert client.get("/").status_code == 200
    client.cookies.set("__Host-spotifast", other_device)
    assert client.get("/").status_code == 303


def test_logout_requires_csrf_and_kills_session(app, client):
    make_user(app, "camille")
    login(client, "camille")
    token = client.cookies.get("__Host-spotifast")
    assert client.post("/logout", data={"csrf": "forged"}).status_code == 403
    assert client.post("/logout", data={"csrf": csrf_of(client)}).status_code == 303
    client.cookies.set("__Host-spotifast", token)
    assert client.get("/").status_code == 303


def test_cross_origin_posts_are_refused(app, client):
    make_user(app, "camille")
    no_origin = client.post("/login", data={"username": "camille", "password": PASSWORD},
                            headers={"Origin": ""})
    assert no_origin.status_code == 403
    evil = client.post("/login", data={"username": "camille", "password": PASSWORD},
                       headers={"Origin": "https://evil.example"})
    assert evil.status_code == 403
    referer_ok = client.post("/login", data={"username": "camille", "password": PASSWORD},
                             headers={"Origin": "", "Referer": ORIGIN + "/login"})
    assert referer_ok.status_code == 303


def test_api_requires_csrf_header(friend):
    r = friend.post("/api/preview", json={"url": "x"})
    assert r.status_code == 403
    r = friend.post("/api/preview", json={"url": "x"}, headers={"X-CSRF-Token": "forged"})
    assert r.status_code == 403
    assert api(friend, "POST", "/api/preview", json={"url": "x"}).status_code == 400


def test_unknown_host_is_refused(client):
    r = client.get("/login", headers={"Host": "evil.example"})
    assert r.status_code == 400


def test_security_headers(client):
    for path in ("/login", "/healthz"):
        r = client.get(path)
        csp = r.headers["content-security-policy"]
        assert "script-src 'self'" in csp and "frame-ancestors 'none'" in csp
        assert "unsafe-inline" not in csp
        assert r.headers["x-content-type-options"] == "nosniff"
        assert r.headers["cache-control"] == "no-store"
        assert r.headers["x-robots-tag"] == "noindex, nofollow"
        # no-referrer would make browsers send `Origin: null` on our own POSTs
        assert r.headers["referrer-policy"] == "same-origin"
    assert client.get("/openapi.json").status_code in (303, 404)
    assert client.get("/docs").status_code in (303, 404)
