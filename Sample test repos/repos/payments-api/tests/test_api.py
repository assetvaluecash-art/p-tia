from fastapi.testclient import TestClient

from app.api.payments import app

client = TestClient(app)


def test_create_payment():
    r = client.post("/payments", json={"account_id": "ACC1001", "amount": 50, "currency": "EUR", "reference": "INV1"})
    assert r.status_code == 201
    assert r.json()["status"] == "PENDING"


def test_balance():
    r = client.get("/accounts/ACC1001/balance")
    assert r.status_code == 200
    assert r.json()["currency"] == "EUR"


def test_unknown_payment():
    r = client.get("/payments/NOPE")
    assert r.status_code == 404
