from datetime import datetime, timedelta, timezone


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def future(**kwargs) -> str:
    return iso(datetime.now(timezone.utc) + timedelta(**kwargs))


def one_time_payload(**overrides) -> dict:
    body = {
        "name": "send-email",
        "payload": {"task": "noop"},
        "schedule_type": "one_time",
        "run_at": future(hours=1),
        "max_retries": 2,
    }
    body.update(overrides)
    return body


def test_create_one_time_job(client):
    response = client.post("/jobs", json=one_time_payload())
    assert response.status_code == 201

    body = response.json()
    assert body["status"] == "SCHEDULED"
    assert body["next_run_at"] == body["run_at"]
    assert body["retries_left"] == 2


def test_run_at_in_the_past_is_rejected(client):
    response = client.post("/jobs", json=one_time_payload(run_at=iso(
        datetime.now(timezone.utc) - timedelta(minutes=1)
    )))
    assert response.status_code == 422
    assert "future" in response.json()["message"]


def test_interval_must_be_positive(client):
    response = client.post(
        "/jobs",
        json={
            "name": "sync",
            "schedule_type": "interval",
            "interval_seconds": 0,
            "payload": {},
        },
    )
    assert response.status_code == 422


def test_one_time_job_cannot_carry_an_interval(client):
    response = client.post("/jobs", json=one_time_payload(interval_seconds=30))
    assert response.status_code == 422
    assert response.json()["code"] == "validation_error"


def test_interval_job_needs_an_interval(client):
    response = client.post(
        "/jobs", json={"name": "sync", "schedule_type": "interval", "payload": {}}
    )
    assert response.status_code == 422


def test_cron_expression_is_validated(client):
    bad = client.post(
        "/jobs",
        json={"name": "nightly", "schedule_type": "cron", "cron_expression": "nope"},
    )
    assert bad.status_code == 422

    good = client.post(
        "/jobs",
        json={"name": "nightly", "schedule_type": "cron", "cron_expression": "0 2 * * *"},
    )
    assert good.status_code == 201
    assert good.json()["next_run_at"] is not None


def test_idempotency_key_prevents_duplicates(client):
    body = one_time_payload(idempotency_key="order-123")

    first = client.post("/jobs", json=body)
    second = client.post("/jobs", json=body)

    assert first.status_code == 201
    assert second.status_code == 200  
    assert first.json()["id"] == second.json()["id"]

    assert client.get("/jobs").json()["total"] == 1


def test_list_filters_by_status_and_type(client):
    client.post("/jobs", json=one_time_payload(name="a"))
    client.post(
        "/jobs",
        json={
            "name": "b",
            "schedule_type": "interval",
            "interval_seconds": 30,
            "payload": {},
        },
    )

    assert client.get("/jobs", params={"schedule_type": "interval"}).json()["total"] == 1
    assert client.get("/jobs", params={"status": "SCHEDULED"}).json()["total"] == 2
    assert client.get("/jobs", params={"status": "COMPLETED"}).json()["total"] == 0


def test_list_filters_by_next_execution_time(client):
    client.post("/jobs", json=one_time_payload(name="soon", run_at=future(minutes=5)))
    client.post("/jobs", json=one_time_payload(name="later", run_at=future(days=2)))

    due_today = client.get("/jobs", params={"next_run_before": future(hours=6)}).json()
    assert due_today["total"] == 1
    assert due_today["items"][0]["name"] == "soon"


def test_job_detail_includes_execution_history(client):
    job_id = client.post("/jobs", json=one_time_payload()).json()["id"]

    detail = client.get(f"/jobs/{job_id}").json()
    assert detail["last_execution"] is None
    assert detail["recent_executions"] == []
    assert detail["attempt_count"] == 0


def test_unknown_job_is_a_404(client):
    response = client.get("/jobs/11111111-1111-1111-1111-111111111111")
    assert response.status_code == 404
    assert response.json()["code"] == "job_not_found"


def test_pause_and_resume(client):
    job_id = client.post("/jobs", json=one_time_payload()).json()["id"]

    assert client.post(f"/jobs/{job_id}/pause").json()["status"] == "PAUSED"
    assert client.post(f"/jobs/{job_id}/resume").json()["status"] == "SCHEDULED"


def test_cannot_resume_a_completed_job(client, session):
    from app.domain.enums import JobStatus
    from app.domain.models import Job

    job_id = client.post("/jobs", json=one_time_payload()).json()["id"]
    job = session.get(Job, __import__("uuid").UUID(job_id))
    job.status = JobStatus.COMPLETED.value
    session.commit()

    response = client.post(f"/jobs/{job_id}/resume")
    assert response.status_code == 409
    assert response.json()["code"] == "illegal_transition"


def test_health_endpoint(client):
    assert client.get("/health").json() == {"status": "ok", "database": True}
