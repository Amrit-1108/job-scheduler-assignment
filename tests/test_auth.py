def test_register_and_login(anon_client):
    created = anon_client.post(
        "/auth/register", json={"username": "alice", "password": "supersecret123"}
    )
    assert created.status_code == 201
    assert created.json()["username"] == "alice"
    assert "password" not in created.json()  

    token = anon_client.post(
        "/auth/token", json={"username": "alice", "password": "supersecret123"}
    )
    assert token.status_code == 200
    assert token.json()["token_type"] == "bearer"
    assert token.json()["access_token"]


def test_username_is_taken_only_once(anon_client):
    body = {"username": "bob", "password": "supersecret123"}
    assert anon_client.post("/auth/register", json=body).status_code == 201
    duplicate = anon_client.post("/auth/register", json=body)
    assert duplicate.status_code == 409
    assert duplicate.json()["code"] == "username_taken"


def test_short_password_is_rejected(anon_client):
    response = anon_client.post(
        "/auth/register", json={"username": "carol", "password": "short"}
    )
    assert response.status_code == 422


def test_wrong_password_is_a_401(anon_client):
    anon_client.post(
        "/auth/register", json={"username": "dave", "password": "supersecret123"}
    )
    response = anon_client.post(
        "/auth/token", json={"username": "dave", "password": "wrong-password"}
    )
    assert response.status_code == 401
    assert response.json()["message"] == "Incorrect username or password"


def test_unknown_user_gets_the_same_401(anon_client):
    response = anon_client.post(
        "/auth/token", json={"username": "ghost", "password": "supersecret123"}
    )
    assert response.status_code == 401
    assert response.json()["message"] == "Incorrect username or password"


def test_jobs_require_a_token(anon_client):
    assert anon_client.get("/jobs").status_code == 401
    assert anon_client.post("/jobs", json={"name": "x", "schedule_type": "interval"}).status_code == 401


def test_garbage_token_is_rejected(anon_client):
    anon_client.headers.update({"Authorization": "Bearer not-a-real-jwt"})
    assert anon_client.get("/jobs").status_code == 401


def test_token_signed_with_the_wrong_secret_is_rejected(anon_client):
    import jwt as pyjwt

    forged = pyjwt.encode(
        {"sub": "11111111-1111-1111-1111-111111111111"}, "attacker-secret", algorithm="HS256"
    )
    anon_client.headers.update({"Authorization": f"Bearer {forged}"})
    assert anon_client.get("/jobs").status_code == 401


def test_me_returns_the_logged_in_user(client):
    assert client.get("/auth/me").json()["username"] == "tester"


def test_health_stays_public(anon_client):
    assert anon_client.get("/health").status_code == 200


def test_users_cannot_see_each_others_jobs(client, other_client):
    """The point of the whole exercise: one account's jobs are invisible and
    untouchable from another account."""
    mine = client.post(
        "/jobs",
        json={"name": "my-job", "schedule_type": "interval", "interval_seconds": 60},
    ).json()

    assert other_client.get("/jobs").json()["total"] == 0

    assert other_client.get(f"/jobs/{mine['id']}").status_code == 404
    assert other_client.post(f"/jobs/{mine['id']}/pause").status_code == 404

    assert client.get("/jobs").json()["total"] == 1


def test_idempotency_keys_are_scoped_per_user(client, other_client):
    body = {
        "name": "shared-key",
        "schedule_type": "interval",
        "interval_seconds": 60,
        "idempotency_key": "same-key",
    }
    mine = client.post("/jobs", json=body)
    theirs = other_client.post("/jobs", json=body)

    assert mine.status_code == 201
    assert theirs.status_code == 201
    assert mine.json()["id"] != theirs.json()["id"]

    again = client.post("/jobs", json=body)
    assert again.status_code == 200
    assert again.json()["id"] == mine.json()["id"]
