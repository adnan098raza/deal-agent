from datetime import datetime, timezone

import pytest

from deal_agent.models import Deal, format_time_left
from deal_agent.parsing import (compute_discount, detect_brand, detect_category, enrich, find_prices,
                                parse_discount, parse_expiry, parse_original_price, parse_price,
                                split_temperature)


@pytest.mark.parametrize("text,expected", [
    ("Sony WH-1000XM5 für 249€", 249.0),
    ("ASUS VivoBook für 549 € – MediaMarkt", 549.0),
    ("345,50€", 345.5),
    ("TV für 1.299,99 €", 1299.99),
    ("TV für 1.299€", 1299.0),
    ("Nur EUR 19,99 heute", 19.99),
    ("EUR 379,00", 379.0),
    ("ab 179,99 €", 179.99),
    ("Preis 12.95€", 12.95),
    ("Kein Preis hier", None),
])
def test_parse_price(text, expected):
    assert parse_price(text) == expected


def test_original_price_and_discount():
    t = "Nike Air Max für 89,99€ statt 149,99€ (-40%)"
    assert find_prices(t) == [89.99, 149.99]
    assert parse_original_price(t) == 149.99
    assert parse_discount(t) == 40
    assert parse_discount("- 14 %") == 14                       # idealo badge
    assert parse_discount("Bisheriger Preis: EUR 1.299,00 71% Rabatt") == 71   # eBay
    assert parse_original_price("2 Stück statt 3 Stück") is None
    assert compute_discount(75, 100) == 25.0
    assert compute_discount(100, 90) is None


def test_temperature_prefix():
    assert split_temperature("345° - Sony WH-1000XM5") == (345, "Sony WH-1000XM5")
    assert split_temperature("-12° - Kalter Deal") == (-12, "Kalter Deal")
    assert split_temperature("Kein Prefix") == (None, "Kein Prefix")


def test_expiry():
    now = datetime(2026, 11, 8, 10, 0, tzinfo=timezone.utc)
    assert parse_expiry("Angebot gültig bis 12.11.", now=now) == datetime(2026, 11, 12, 22, 59, tzinfo=timezone.utc)
    assert parse_expiry("Aktion läuft bis zum 15.11.2026 um 18:00 Uhr", now=now) == \
        datetime(2026, 11, 15, 17, 0, tzinfo=timezone.utc)
    assert parse_expiry("Gutschein bis 03.01.", now=datetime(2026, 12, 20, tzinfo=timezone.utc)).year == 2027
    assert parse_expiry("nur heute!", now=now).date() == now.date()
    assert parse_expiry("no date here", now=now) is None
    assert parse_expiry("BOSS -30 % extra 08.11.2026 - 09.11.2026", now=now) == \
        datetime(2026, 11, 9, 22, 59, tzinfo=timezone.utc)                   # range -> end date
    assert parse_expiry("Aktion 01.11. – 14.11.2026", now=now).day == 14


def test_brand_and_category(prefs):
    assert detect_brand("The North Face Jacke Herren", prefs.all_brands) == "The North Face"
    assert detect_brand("Thomas Sabo Herrenkette Silber", prefs.all_brands) == "Thomas Sabo"
    assert detect_brand("Sonyx Gerät", prefs.all_brands) is None
    c = prefs.categories
    assert detect_category("Bose QuietComfort Kopfhörer", c) == "electronics"
    assert detect_category("Levi's 501 Jeans Herren", c) == "clothing"
    assert detect_category("Adidas Samba OG", c) == "shoes"
    assert detect_category("Nike Air Force 1 '07", c) == "shoes"
    assert detect_category("Tissot PRX Automatikuhr", c) == "jewelry_accessories"
    assert detect_category("Siegelring Edelstahl", c) == "jewelry_accessories"
    assert detect_category("CeraVe Feuchtigkeitscreme", c) == "cosmetics"
    assert detect_category("Dior Sauvage Eau de Parfum 100ml", c) == "cosmetics"
    assert detect_category("Gartenschlauch 20m", c) is None


def test_enrich(prefs):
    d = Deal(title="245° - Sony WH-1000XM5 Kopfhörer für 249€ statt 329€", link="https://x/1", source="t")
    enrich(d, prefs)
    assert (d.temperature, d.price, d.original_price, d.brand, d.category) == (245, 249, 329, "Sony", "electronics")
    assert d.discount_pct == pytest.approx(24.3)
    assert d.title.startswith("Sony")


def test_time_left():
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    assert format_time_left(None) == "unknown"
    assert format_time_left(datetime(2026, 1, 1, 5, 30, tzinfo=timezone.utc), now) == "5h 30m"
    assert format_time_left(datetime(2026, 1, 3, 2, tzinfo=timezone.utc), now) == "2d 2h"
    assert format_time_left(datetime(2025, 12, 31, tzinfo=timezone.utc), now) == "expired"
