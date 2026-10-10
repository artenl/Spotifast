import re

from conftest import PASSWORD, csrf_of, login, make_user

from spotifast import cli


def admin_client(app, client):
    make_user(app, "arthur", admin=True)
    login(client, "arthur")
    return client


def test_members_cannot_reach_admin(app, friend):
    assert friend.get("/admin").status_code == 403
    csrf = csrf_of(friend)
    r = friend.post("/admin/users", data={"csrf": csrf, "username": "pirate", "is_admin": "1"})
    assert r.status_code == 403
    assert app.state.db.get_user_by_name("pirate") is None


def test_admin_creates_a_member_with_a_one_time_password(app, client):
    admin = admin_client(app, client)
    r = admin.post("/admin/users", data={"csrf": csrf_of(admin), "username": "camille"})
    assert r.status_code == 200
    password = re.search(r'<code id="temp-pass">([^<]+)</code>', r.text).group(1)
    user = app.state.db.get_user_by_name("camille")
    assert user and not user.is_admin and user.must_change_password

    admin.cookies.clear()
    assert login(admin, "camille", password).headers["location"] == "/account"


def test_admin_rejects_bad_or_duplicate_usernames(app, client):
    admin = admin_client(app, client)
    csrf = csrf_of(admin)
    for name in ("a", "<script>", "x" * 40, "arthur", "ARTHUR"):
        r = admin.post("/admin/users", data={"csrf": csrf, "username": name})
        assert r.status_code == 400, name
        assert "<script>" not in r.text.replace("&lt;script&gt;", "")


def test_admin_post_requires_csrf(app, client):
    admin = admin_client(app, client)
    r = admin.post("/admin/users", data={"csrf": "forged", "username": "camille"})
    assert r.status_code == 403
    assert app.state.db.get_user_by_name("camille") is None


def test_disabling_a_member_logs_them_out(app, client):
    from fastapi.testclient import TestClient

    from conftest import ORIGIN

    member = make_user(app, "camille")
    with TestClient(app, base_url=ORIGIN, headers={"Origin": ORIGIN},
                    follow_redirects=False) as other:
        login(other, "camille")
        assert other.get("/").status_code == 200
        admin = admin_client(app, client)
        r = admin.post(f"/admin/users/{member.id}/active", data={"csrf": csrf_of(admin),
                                                                 "active": "0"})
        assert r.status_code == 303
        assert other.get("/").status_code == 303


def test_admin_cannot_remove_themselves(app, client):
    admin = admin_client(app, client)
    me = app.state.db.get_user_by_name("arthur")
    csrf = csrf_of(admin)
    assert admin.post(f"/admin/users/{me.id}/delete", data={"csrf": csrf}).status_code == 403
    assert admin.post(f"/admin/users/{me.id}/active",
                      data={"csrf": csrf, "active": "0"}).status_code == 403


def test_reset_password_revokes_sessions(app, client):
    member = make_user(app, "camille")
    admin = admin_client(app, client)
    r = admin.post(f"/admin/users/{member.id}/reset", data={"csrf": csrf_of(admin)})
    assert r.status_code == 200
    admin.cookies.clear()
    assert login(admin, "camille", PASSWORD).status_code == 401


def test_cli_bootstrap(settings, monkeypatch, capsys):
    monkeypatch.setenv("SPOTIFAST_DATA_DIR", str(settings.data_dir))
    assert cli.main(["create-user", "arthur", "--admin"]) == 0
    out = capsys.readouterr().out
    assert "temporary password:" in out
    assert cli.main(["create-user", "arthur"]) == 1
    assert cli.main(["list-users"]) == 0
    assert "arthur\tadmin,must-change-password" in capsys.readouterr().out
