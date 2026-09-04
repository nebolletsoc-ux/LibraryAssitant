"""Tests for the library configuration endpoints."""


def test_get_libraries_returns_defaults(client):
    resp = client.get("/api/libraries")
    assert resp.status_code == 200
    data = resp.get_json()
    keys = [lib["library_key"] for lib in data]
    # LAPL is the only prepopulated library.
    assert keys == ["lapl"]
    assert data[0]["label"] == "Los Angeles Public Library"
    # Defaults are all disabled in fixtures.
    assert all(lib["enabled"] is False for lib in data)


def test_get_libraries_always_has_user1(client, app_context):
    from models import LibraryConfig, db
    resp = client.get("/api/libraries")
    assert resp.status_code == 200
    assert len(resp.get_json()) == LibraryConfig.query.filter_by(user_id=1).count()


def test_update_library_enables_it(client):
    libs = client.get("/api/libraries").get_json()
    lapl = next(l for l in libs if l["library_key"] == "lapl")

    resp = client.patch(f"/api/libraries/{lapl['id']}", json={"enabled": True})
    assert resp.status_code == 200
    assert resp.get_json()["enabled"] is True

    after = client.get("/api/libraries").get_json()
    assert next(l for l in after if l["id"] == lapl["id"])["enabled"] is True


def test_update_library_disables_it(client):
    libs = client.get("/api/libraries").get_json()
    id_ = libs[0]["id"]
    client.patch(f"/api/libraries/{id_}", json={"enabled": True})
    resp = client.patch(f"/api/libraries/{id_}", json={"enabled": False})
    assert resp.status_code == 200
    assert resp.get_json()["enabled"] is False


def test_update_missing_library_returns_404(client):
    resp = client.patch("/api/libraries/999999", json={"enabled": True})
    assert resp.status_code == 404


def test_update_with_no_body_keeps_current(client):
    libs = client.get("/api/libraries").get_json()
    id_ = libs[0]["id"]
    resp = client.patch(f"/api/libraries/{id_}", json={})
    assert resp.status_code == 200
    # No "enabled" key sent -> retains the existing (False) value.
    assert resp.get_json()["enabled"] is False


# ---------- add library (POST /api/libraries) ----------

def test_add_preset_already_configured_returns_409(client):
    resp = client.post("/api/libraries", json={"library_key": "lapl"})
    assert resp.status_code == 409
    assert "already configured" in resp.get_json()["error"]


def test_add_custom_overdrive_library(client):
    resp = client.post("/api/libraries", json={
        "library_key": "my_lib",
        "label": "My Local Library",
        "overdrive": "mylocal",
    })
    assert resp.status_code == 201
    data = resp.get_json()
    assert data["label"] == "My Local Library"
    assert data["overdrive"] == "mylocal"
    assert data["enabled"] is True


def test_add_custom_library_without_connection_returns_400(client):
    resp = client.post("/api/libraries", json={"library_key": "x", "label": "X"})
    assert resp.status_code == 400


def test_add_custom_library_requires_key(client):
    resp = client.post("/api/libraries", json={"label": "No key"})
    assert resp.status_code == 400


def test_add_duplicate_custom_library_returns_409(client):
    payload = {"library_key": "dup", "label": "Dup", "bibliocommons": "dup"}
    assert client.post("/api/libraries", json=payload).status_code == 201
    resp = client.post("/api/libraries", json=payload)
    assert resp.status_code == 409


# ---------- available libraries (GET /api/libraries/available) ----------

def test_available_libraries_are_presets_not_configured(client):
    resp = client.get("/api/libraries/available")
    assert resp.status_code == 200
    data = resp.get_json()
    assert isinstance(data, list)
    for lib in data:
        assert "library_key" in lib and "label" in lib


def test_available_libraries_present_when_lapl_not_configured(client, app_context):
    from models import LibraryConfig, db

    with app_context.app.app_context():
        LibraryConfig.query.filter_by(user_id=1).delete()
        db.session.commit()

    data = client.get("/api/libraries/available").get_json()
    keys = [lib["library_key"] for lib in data]
    assert "lapl" in keys
    assert next(lib for lib in data if lib["library_key"] == "lapl")["label"] == "Los Angeles Public Library"


def test_available_libraries_empty_when_all_seeded(client, app_context):
    from models import LibraryConfig, db

    with app_context.app.app_context():
        for config in LibraryConfig.query.filter_by(user_id=1).all():
            if config.library_key != "lapl":
                db.session.delete(config)
        db.session.commit()

    data = client.get("/api/libraries/available").get_json()
    assert data == []
