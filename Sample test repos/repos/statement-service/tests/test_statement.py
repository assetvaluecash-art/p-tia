from fastapi.testclient import TestClient

from app import app

client = TestClient(app)


def test_statement_pdf():
    r = client.get("/statements/ACC1001/pdf")
    assert r.status_code == 200
