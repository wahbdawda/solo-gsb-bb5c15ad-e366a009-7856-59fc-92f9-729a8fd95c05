import os
import tempfile

# 必须在导入 app 之前设置，保证测试使用独立的临时数据库
_fd, _path = tempfile.mkstemp(suffix=".db")
os.close(_fd)
os.environ["DB_PATH"] = _path

import pytest
from fastapi.testclient import TestClient

from app.db import get_conn, init_db, write_tx
from app.main import app


@pytest.fixture(autouse=True)
def clean_db():
    init_db()
    with write_tx() as conn:
        conn.execute("DELETE FROM corrections")
        conn.execute("DELETE FROM finalized")
        conn.execute("DELETE FROM fragments")
        conn.execute("DELETE FROM gaps")
        conn.execute("DELETE FROM gap_decisions")
        conn.execute("DELETE FROM channels")
    yield


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture
def conn():
    return get_conn()
