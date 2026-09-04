import os
import sys
import tempfile

import pytest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

# Point the app at a throwaway on-disk SQLite database BEFORE importing it.
# app.py calls db.create_all() and seeds the default library configs at
# import time, so the DB location must be set before the module loads.
_tmp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
os.environ["DATABASE_URL"] = f"sqlite:///{_tmp_db.name}"


@pytest.fixture(scope="session")
def app_context():
    import app as app_module

    with app_module.app.app_context():
        yield app_module


@pytest.fixture()
def client(app_context):
    app_module = app_context
    yield app_module.app.test_client()


@pytest.fixture(autouse=True)
def _reset_db(app_context):
    """Reset all tables between tests so each test starts clean."""
    from models import Book, UserBook, Availability, LibraryConfig, db

    app_module = app_context
    with app_module.app.app_context():
        db.session.remove()
        for model in (Availability, UserBook, Book, LibraryConfig):
            db.session.query(model).delete()
        db.session.commit()

        # Re-seed the default library configs just like app startup does.
        from app import LIBRARY_PRESETS
        seeded = []
        for key, preset in LIBRARY_PRESETS.items():
            config = LibraryConfig(
                user_id=1,
                library_key=key,
                label=preset.get("label", key),
                bibliocommons=preset.get("bibliocommons"),
                overdrive=preset.get("overdrive"),
                hoopla=bool(preset.get("hoopla")),
                enabled=False,
            )
            db.session.add(config)
            seeded.append(config)
        db.session.commit()

        yield


@pytest.fixture(autouse=True)
def _mock_network(monkeypatch):
    """Prevent tests from hitting real library/Open Library endpoints.

    search_libraries is replaced so availability checks are deterministic
    and offline. The sheet-matching helpers are tested against fixtures
    directly, while endpoint-level tests use the stubbed search_libraries.
    """
    import app as app_module

    default_results = []

    def fake_search_libraries(title, author, library_configs, timeout=15):
        return list(default_results)

    # Expose the stub for tests to reconfigure through the fixture.
    monkeypatch.setattr(app_module, "search_libraries", fake_search_libraries)
    yield default_results


class _ImmediateExecutor:
    def submit(self, fn, *args, **kwargs):
        fn(*args, **kwargs)


@pytest.fixture()
def make_result():
    """Factory for a LibraryResult, mirroring the shape search code returns."""
    from library.models import LibraryResult

    def _make(**kwargs):
        defaults = {
            "library": "berkeley",
            "provider": "Libby",
            "format": "eBook",
            "available": True,
            "wait": None,
            "url": "https://example.com/media/1",
            "holds": None,
            "wait_weeks": None,
        }
        defaults.update(kwargs)
        return LibraryResult(**defaults)

    return _make


@pytest.fixture()
def add_book(client):
    """Returns a helper that adds a book and returns the JSON response."""
    def _add(payload):
        return client.post("/api/books", json=payload)
    return _add
