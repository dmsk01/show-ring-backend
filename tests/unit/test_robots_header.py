from fastapi.testclient import TestClient

from app.main import app


def test_api_responses_are_noindex():
    with TestClient(app) as client:
        r = client.get("/captcha/challenge")
    assert r.headers["X-Robots-Tag"] == "noindex, nofollow"
