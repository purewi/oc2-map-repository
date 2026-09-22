from client.variant import current_variant


def test_cos_variant_is_default(monkeypatch) -> None:
    monkeypatch.delenv("OC2_CLIENT_VARIANT", raising=False)
    variant = current_variant()
    assert variant.key == "cos"
    assert variant.cos_enabled


def test_public_variant_disables_cos(monkeypatch) -> None:
    monkeypatch.setenv("OC2_CLIENT_VARIANT", "public")
    variant = current_variant()
    assert variant.key == "public"
    assert not variant.cos_enabled
