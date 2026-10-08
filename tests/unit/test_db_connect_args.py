from app.config import settings
from app.database import engine_connect_args


def test_statement_timeout_passed_to_asyncpg(monkeypatch):
    monkeypatch.setattr(settings, "db_statement_timeout_ms", 7000)
    assert engine_connect_args(settings) == {
        "server_settings": {"statement_timeout": "7000"}
    }


def test_zero_disables_timeout(monkeypatch):
    monkeypatch.setattr(settings, "db_statement_timeout_ms", 0)
    assert engine_connect_args(settings) == {}
