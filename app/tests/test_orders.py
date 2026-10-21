from app.flags import flags


def test_health(client):
    body = client.get("/health").json()
    assert body["status"] == "ok"
    assert "new_pricing" in body["flags"]


def test_quote_legacy_pricing(client):
    r = client.post("/orders/quote", json={"items": [{"sku": "mug", "quantity": 2}, {"sku": "tea", "quantity": 3}]})
    assert r.status_code == 200
    assert r.json()["subtotal_cents"] == 2 * 1200 + 3 * 400
    assert r.json()["total_cents"] == r.json()["subtotal_cents"]
    assert r.json()["pricing"] == "legacy"


def test_new_pricing_path_runs_only_behind_flag(client):
    flags.override("new_pricing", True)
    r = client.post("/orders/quote", json={"items": [{"sku": "tea", "quantity": 1}]})
    assert r.json()["pricing"] == "new"


def test_launch_promo_typical_order(client):
    flags.override("new_pricing", True)
    r = client.post("/orders/quote", json={"items": [{"sku": "mug", "quantity": 1}, {"sku": "beans", "quantity": 1}]})
    body = r.json()
    assert body["subtotal_cents"] == 3000
    assert body["total_cents"] == 3000 - 1500
    assert body["pricing"] == "new"


def test_launch_promo_never_discounts_more_than_half(client):
    flags.override("new_pricing", True)
    r = client.post("/orders/quote", json={"items": [{"sku": "tea", "quantity": 1}]})
    body = r.json()
    assert body["subtotal_cents"] == 400
    assert body["total_cents"] == 200  # capped at 50%, not $15 off


def test_launch_promo_large_order(client):
    flags.override("new_pricing", True)
    r = client.post("/orders/quote", json={"items": [{"sku": "beans", "quantity": 5}]})
    assert r.json()["total_cents"] == 9000 - 1500


def test_create_and_get_order(client):
    r = client.post("/orders", json={"customer_id": "c1", "items": [{"sku": "beans", "quantity": 1}]})
    assert r.status_code == 201
    order = client.get(f"/orders/{r.json()['id']}").json()
    assert order["total_cents"] == 1800


def test_validation(client):
    assert client.post("/orders/quote", json={"items": [{"sku": "nope", "quantity": 1}]}).status_code == 422
    assert client.post("/orders/quote", json={"items": [{"sku": "tea", "quantity": 0}]}).status_code == 422
    assert client.post("/orders/quote", json={"items": []}).status_code == 422
    assert client.get("/orders/999999").status_code == 404


def test_storefront_page(client):
    r = client.get("/")
    assert r.status_code == 200 and "Rewind demo shop" in r.text
    assert "orders/quote" in r.text  # prices come from this revision's own API
