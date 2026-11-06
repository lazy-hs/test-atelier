from api.tests.test_projects import client, login  # noqa: F401


def test_shell_pilot_fails_closed_and_does_not_grant_project_roles(client, monkeypatch):
    path = "/api/v1/harness/web-access"
    assert client.get(path).status_code == 401
    admin = login(client)
    assert client.get(path).status_code == 404

    created = client.post("/api/v1/users", json={
        "username": "pilot-reader", "password": "pilot-reader-password-123", "role": "reader",
    }, headers=admin)
    assert created.status_code == 200
    monkeypatch.setenv("TEST_ATELIER_WEB_SHELL_PILOT", "1")
    allowed = client.get(path)
    assert allowed.status_code == 200 and allowed.json() == {"allowed": True}
    client.post("/api/v1/auth/logout", headers=admin)
    login(client, "pilot-reader", "pilot-reader-password-123")
    assert client.get(path).status_code == 403


def test_logout_revokes_the_old_signed_session_for_shell_access(client, monkeypatch):
    path = "/api/v1/harness/web-access"
    monkeypatch.setenv("TEST_ATELIER_WEB_SHELL_PILOT", "1")
    admin = login(client)
    old_cookie = client.cookies.get("session")
    assert old_cookie
    assert client.get("/api/v1/auth/me").status_code == 200
    assert client.post("/api/v1/auth/logout", headers=admin).status_code == 200

    # Reattach the cookie that logout cannot physically erase from an already
    # open WebSocket; the server-side nonce must still reject it.
    client.cookies.set("session", old_cookie)
    assert client.get("/api/v1/auth/me").status_code == 401
    assert client.get(path).status_code == 401


def test_opt_in_member_access_requires_an_active_project_membership(client, monkeypatch):
    path = "/api/v1/harness/web-access"
    monkeypatch.setenv("TEST_ATELIER_WEB_SHELL_PILOT", "1")
    monkeypatch.setenv("TEST_ATELIER_WEB_SHELL_MEMBER_ACCESS", "1")
    monkeypatch.setenv("TEST_ATELIER_WEB_SHELL_RESTRICTED_PROFILE", "1")
    admin = login(client)
    project = client.post("/api/v1/projects", json={"name": "Shell member project"}, headers=admin)
    assert project.status_code == 200, project.text
    project_id = project.json()["id"]
    member = client.post("/api/v1/users", json={
        "username": "shell-member", "password": "shell-member-password-123", "role": "reader",
    }, headers=admin)
    assert member.status_code == 200, member.text
    member_id = member.json()["id"]
    assert client.post(
        f"/api/v1/projects/{project_id}/members",
        json={"user_id": member_id, "role": "reader"}, headers=admin,
    ).status_code == 200
    unassigned = client.post("/api/v1/users", json={
        "username": "shell-unassigned", "password": "shell-unassigned-password-123", "role": "reader",
    }, headers=admin)
    assert unassigned.status_code == 200, unassigned.text

    client.post("/api/v1/auth/logout", headers=admin)
    login(client, "shell-member", "shell-member-password-123")
    assert client.get(path).status_code == 200
    client.post("/api/v1/auth/logout")
    login(client, "shell-unassigned", "shell-unassigned-password-123")
    assert client.get(path).status_code == 403

    # The member switch cannot accidentally open an unrestricted upstream
    # profile; deployment must attest that the ta-restricted profile is active.
    monkeypatch.delenv("TEST_ATELIER_WEB_SHELL_RESTRICTED_PROFILE", raising=False)
    login(client, "shell-member", "shell-member-password-123")
    assert client.get(path).status_code == 403
