import os

import pytest
from fastapi.testclient import TestClient

from app.db import init_db
from app.main import app


@pytest.fixture()
def db_path(tmp_path):
    db = tmp_path / "test.db"
    os.environ["DATABASE_PATH"] = str(db)
    init_db(str(db))
    return str(db)


@pytest.fixture()
def client(db_path):
    with TestClient(app) as c:
        yield c


@pytest.fixture()
def authed(client):
    r = client.post("/api/login", json={"username": "admin", "password": "123456"})
    assert r.status_code == 200
    return client
