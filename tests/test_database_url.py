"""DATABASE_URL normalization for persistent Postgres storage."""

from app import _normalize_database_url


def test_unset_returns_none():
    assert _normalize_database_url(None) is None
    assert _normalize_database_url("") is None
    assert _normalize_database_url("   ") is None


def test_postgres_scheme_gets_psycopg2_dialect():
    url = _normalize_database_url("postgres://user:pw@host:5432/db")
    assert url.startswith("postgresql+psycopg2://")
    assert "user:pw@host:5432/db" in url


def test_postgresql_scheme_gets_psycopg2_dialect():
    url = _normalize_database_url("postgresql://user:pw@host:5432/db?sslmode=require")
    assert url.startswith("postgresql+psycopg2://")
    assert "sslmode=require" in url


def test_existing_psycopg2_dialect_is_untouched():
    url = _normalize_database_url("postgresql+psycopg2://user:pw@host/db")
    assert url == "postgresql+psycopg2://user:pw@host/db"


def test_other_schemes_are_left_alone():
    assert _normalize_database_url("mysql://user:pw@host/db") == "mysql://user:pw@host/db"
    assert _normalize_database_url("sqlite:///foo.db") == "sqlite:///foo.db"