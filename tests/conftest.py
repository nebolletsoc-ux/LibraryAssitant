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


class AuthedClient:
    """Flask test client that echoes the CSRF cookie as a request header.

    Mirrors what the browser does: the server drops a non-HttpOnly csrf_token
    cookie and the JS sends it back as X-CSRF-Token on state-changing /api
    requests. Set ``client.csrf_token = None`` in a test to simulate a client
    that never got a token (exercise the CSRF rejection path).
    """

    def __init__(self, base):
        self._base = base
        self.csrf_token = None

    def __getattr__(self, name):
        return getattr(self._base, name)

    def open(self, *args, **kwargs):
        method = (kwargs.pop("method", None) or "GET").upper()
        path = kwargs.get("path", args[0] if args else "/")
        if (
            method in ("POST", "PUT", "PATCH", "DELETE")
            and str(path).startswith("/api/")
            and self.csrf_token
        ):
            headers = dict(kwargs.get("headers") or {})
            headers.setdefault("X-CSRF-Token", self.csrf_token)
            kwargs["headers"] = headers
        return self._base.open(*args, method=method, **kwargs)

    def get(self, path, **kw):
        return self.open(path, method="GET", **kw)

    def post(self, path, **kw):
        return self.open(path, method="POST", **kw)

    def put(self, path, **kw):
        return self.open(path, method="PUT", **kw)

    def patch(self, path, **kw):
        return self.open(path, method="PATCH", **kw)

    def delete(self, path, **kw):
        return self.open(path, method="DELETE", **kw)


@pytest.fixture(scope="session")
def app_context():
    import app as app_module

    with app_module.app.app_context():
        yield app_module


@pytest.fixture()
def raw_client(app_context):
    """Unauthenticated, CSRF-free client for auth-specific tests."""
    app_module = app_context
    yield app_module.app.test_client()


@pytest.fixture()
def client(app_context):
    app_module = app_context
    yield AuthedClient(app_module.app.test_client())


@pytest.fixture(autouse=True)
def _reset_db(request, app_context, client):
    """Reset all tables between tests so each test starts clean.

    By default also creates a "tester" account, logs it in, and seeds the
    nine shipped library presets for it, so data-scoped endpoints behave as
    before the auth work. Mark a test with ``@pytest.mark.no_auth`` to opt
    out of the account so auth/registration tests start from an empty DB.
    """
    from werkzeug.security import generate_password_hash

    from models import Book, UserBook, Availability, LibraryConfig, User, db
    from app import LIBRARY_PRESETS

    no_auth = request.node.get_closest_marker("no_auth") is not None

    with app_context.app.app_context():
        db.session.remove()
        for model in (Availability, UserBook, Book, LibraryConfig, User):
            db.session.query(model).delete()
        db.session.commit()

        if not no_auth:
            user = User(
                username="tester",
                password_hash=generate_password_hash("testpassword"),
            )
            db.session.add(user)
            db.session.commit()
            user_id = user.id

            for key, preset in LIBRARY_PRESETS.items():
                db.session.add(LibraryConfig(
                    user_id=user_id,
                    library_key=key,
                    label=preset.get("label", key),
                    bibliocommons=preset.get("bibliocommons"),
                    overdrive=preset.get("overdrive"),
                    hoopla=bool(preset.get("hoopla")),
                    enabled=False,
                ))
            db.session.commit()

    if not no_auth:
        with client.session_transaction() as sess:
            sess["user_id"] = user_id

    # Every wrapper gets a CSRF cookie/token pair matching a real browser.
    token = "test-csrf-token"
    client.set_cookie("csrf_token", token)
    client.csrf_token = token

    yield


@pytest.fixture()
def current_user_id(app_context):
    """The id of the auto-created tester account."""
    from models import User

    with app_context.app.app_context():
        return User.query.filter_by(username="tester").one().id


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