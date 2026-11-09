import copy
import yaml

import pandas as pd
import pytest
from fastapi.testclient import TestClient

from tradealerts import news, pricealerts
from tradealerts.sample import synthetic_bars
from tradealerts.state import StateStore
from tradealerts.web import create_app

from .test_web import CSV_STRATEGY, H


@pytest.fixture
def ws(tmp_path):
    (tmp_path / "strategies").mkdir()
    (tmp_path / "data").mkdir()
    synthetic_bars(days=240, freq="1h", seed=4).to_csv(tmp_path / "data" / "bars.csv")
    (tmp_path / "config.yaml").write_text("poll_minutes: 15\nnotifiers:\n  - type: console\n")
    return tmp_path


@pytest.fixture
def client(ws):
    return TestClient(create_app(ws / "config.yaml"))


def test_price_alert_crud_and_trigger(client, ws):
    assert client.post("/api/price-alerts", json={"market": "nope", "direction": "above", "price": 1}, headers=H).status_code == 422
    assert client.post("/api/price-alerts", json={"market": "gold", "direction": "above"}, headers=H).status_code == 422
    a = client.post("/api/price-alerts", json={"market": "gold", "direction": "above", "price": 2700, "note": "breakout"},
                    headers=H).json()
    b = client.post("/api/price-alerts", json={"market": "gold", "direction": "below", "price": 2500}, headers=H).json()
    assert a["label"] == "Gold" and a["active"] == 1
    assert len(client.get("/api/price-alerts?market=gold").json()["alerts"]) == 2

    store = StateStore(ws / "data" / "state.db")
    created = pd.Timestamp(a["created_at"])
    idx = pd.date_range(created.floor("5min") - pd.Timedelta(minutes=30), periods=20, freq="5min")
    bars = pd.DataFrame({"open": 2650.0, "high": 2660.0, "low": 2640.0, "close": 2650.0}, index=idx)
    bars.iloc[12, bars.columns.get_loc("high")] = 2705.0  # touches 2,700 after the alert was made
    sent = []

    class N:
        name = "test"
        def send(self, msg):
            sent.append(msg)

    fired = pricealerts.check_price_alerts(None, store, [N()], now=idx[-1] + pd.Timedelta(minutes=5),
                                           loader=lambda cfg, m: bars)
    assert [f["id"] for f in fired] == [a["id"]] and "Gold above 2,700" in sent[0].title
    assert store.price_alert(a["id"])["active"] == 0 and store.price_alert(b["id"])["active"] == 1
    # a price touched before the alert existed doesn't count
    assert pricealerts.hit({"direction": "above", "price": 2600, "created_at": idx[-1].isoformat(), "checked_to": None},
                           bars.assign(high=2610.0).iloc[:-2]) is None
    store.close()
    # re-arm, then delete
    r = client.put(f"/api/price-alerts/{a['id']}", json={"direction": "above", "price": 2750}, headers=H).json()
    assert r["active"] == 1 and r["price"] == 2750 and r["triggered_at"] is None
    client.delete(f"/api/price-alerts/{a['id']}", headers=H)
    assert len(client.get("/api/price-alerts").json()["alerts"]) == 1


def test_journal_with_slippage(client, ws):
    store = StateStore(ws / "data" / "state.db")
    store.add_trade("s1", {"direction": "long", "entry_time": "2025-01-02T10:00:00+00:00", "entry_price": 100.0,
                           "exit_time": "2025-01-02T14:00:00+00:00", "exit_price": 110.0, "size": 1, "pnl": 10.0}, None)
    pid = store.trades()[0]["id"]
    store.close()
    bad = client.post("/api/journal", json={"market": "us500", "direction": "long"}, headers=H)
    assert bad.status_code == 422 and "Fill in" in bad.json()["detail"]
    entry = {"market": "us500", "direction": "long", "entry_time": "2025-01-02T10:01", "entry_price": 100.5,
             "exit_time": "2025-01-02T14:02", "exit_price": 109.0, "size": 2, "fees": 1, "paper_trade_id": pid}
    jid = client.post("/api/journal", json=entry, headers=H).json()["id"]
    client.post("/api/journal", json={"market": "Apple shares", "direction": "short", "entry_time": "2025-02-01T09:00",
                                      "entry_price": 200, "size": 3}, headers=H)
    j = client.get("/api/journal").json()
    e = next(x for x in j["entries"] if x["id"] == jid)
    assert e["pnl"] == pytest.approx((109 - 100.5) * 2 - 1)
    assert e["entry_slippage"] == pytest.approx(0.5) and e["exit_slippage"] == pytest.approx(1.0)
    assert e["slippage_cost"] == pytest.approx(3.0)
    assert j["summary"]["open"] == 1 and j["summary"]["linked"] == 1
    csv = client.get("/api/journal.csv")
    assert csv.headers["content-type"].startswith("text/csv") and "Apple shares" in csv.text
    assert client.put(f"/api/journal/{jid}", json={**entry, "exit_time": None}, headers=H).status_code == 422
    client.delete(f"/api/journal/{jid}", headers=H)
    assert len(client.get("/api/journal").json()["entries"]) == 1


def test_portfolio_of_two_strategies(client):
    a = client.post("/api/strategies", json={"data": CSV_STRATEGY}, headers=H).json()["id"]
    other = copy.deepcopy(CSV_STRATEGY)
    other["name"] = "CSV short"
    other["direction"] = "short"
    other["entry"]["all"][0]["op"] = "crosses_below"
    other["entry"]["all"][0]["right"] = 65
    b = client.post("/api/strategies", json={"data": other}, headers=H).json()["id"]
    assert client.post("/api/portfolio", json={"ids": [a, b]}, headers=H).status_code == 422  # no balance
    r = client.post("/api/portfolio", json={"ids": [a, b], "balance": 5000}, headers=H)
    assert r.status_code == 200, r.text
    out = r.json()
    m = out["metrics"]
    assert m["start_equity"] == 5000 and m["trades"] == sum(p["trades"] for p in out["per_strategy"])
    total = sum(p["pnl"] for p in out["per_strategy"])
    assert m["end_equity"] == pytest.approx(5000 + total, rel=1e-6)
    assert 0 <= m["max_open_risk_pct"] <= 4  # each risks 1% a trade
    assert out["correlation"]["ids"] == [a, b] or out["correlation"] is None


def test_sweep_includes_walk_forward(client):
    body = {"data": CSV_STRATEGY, "setting": {"kind": "path", "path": ["entry", "all", 0, "right"]},
            "values": [25, 30, 35, 40]}
    wf = client.post("/api/sweep", json=body, headers=H).json()["walk_forward"]
    assert len(wf["windows"]) == 4
    assert all(w["value"] in (25, 30, 35, 40) for w in wf["windows"])
    assert wf["hindsight_pct"] >= max(0, wf["walk_forward_pct"]) - 1e-9 or wf["hindsight_pct"] >= wf["walk_forward_pct"]


def test_news_jobs_report_and_rule(client, tmp_path):
    ev = news.us_jobs_reports(pd.Timestamp("2025-03-01", tz="UTC"), pd.Timestamp("2025-03-31", tz="UTC"))
    assert ev["time"].iloc[0] == pd.Timestamp("2025-03-07 13:30", tz="UTC")
    d = copy.deepcopy(CSV_STRATEGY)
    d["entry"]["all"].append({"avoid_news": {"before": 30, "after": 30, "currencies": []}})
    r = client.post("/api/validate", json={"data": d}, headers=H).json()
    assert not r["ok"] and "currencies" in r["error"]
    d["entry"]["all"][-1]["avoid_news"]["currencies"] = ["USD"]
    assert client.post("/api/validate", json={"data": d}, headers=H).json()["ok"]
    # fewer or equal trades with the filter
    plain = client.post("/api/backtest", json={"data": CSV_STRATEGY}, headers=H).json()["trade_count"]
    filtered = client.post("/api/backtest", json={"data": d}, headers=H).json()["trade_count"]
    assert filtered <= plain


def test_news_feed_parsing(tmp_path):
    import io
    import json
    items = [{"title": "CPI m/m", "country": "USD", "date": "2025-03-12T08:30:00-04:00", "impact": "High"},
             {"title": "Bank Holiday", "country": "GBP", "date": "2025-03-12T00:00:00-04:00", "impact": "Holiday"}]

    class Resp(io.BytesIO):
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False

    path = tmp_path / "news_events.csv"
    added = news.update_from_feed(path, opener=lambda req, timeout: Resp(json.dumps(items).encode()), max_age_hours=0)
    assert added == 1
    saved = news.load_file(path)
    assert saved["time"].iloc[0] == pd.Timestamp("2025-03-12 12:30", tz="UTC") and saved["impact"].iloc[0] == "high"
    assert news.update_from_feed(path, opener=lambda *a, **k: 1 / 0) == 0  # fresh: not fetched again


def test_recipe_news_toggle():
    from tradealerts.recipes import build_strategy, example_settings
    d = build_strategy("dip", "gbpusd", example_settings("dip", avoid_news=True))
    assert d["entry"]["all"][-1] == {"avoid_news": {"before": 30, "after": 30, "currencies": ["GBP", "USD"]}}


def test_candles_for_a_strategy(client):
    sid = client.post("/api/strategies", json={"data": CSV_STRATEGY}, headers=H).json()["id"]
    r = client.get(f"/api/candles?strategy={sid}&bars=120")
    assert r.status_code == 200, r.text
    out = r.json()
    assert len(out["candles"]) == 120 and len(out["candles"][0]) == 5
    assert all(m["kind"] == "signal" for m in out["marks"])
    assert client.get("/api/candles?market=nope").status_code == 404


def test_backtest_includes_chart(client):
    out = client.post("/api/backtest", json={"data": CSV_STRATEGY}, headers=H).json()
    assert len(out["chart"]["candles"]) == 300
    assert {m["kind"] for m in out["chart"]["marks"]} <= {"buy", "sell", "exit"}


def test_news_page_notes_and_reminders(client, ws, monkeypatch):
    monkeypatch.setattr(news, "update_from_feed", lambda **k: 0)
    soon = (pd.Timestamp.now(tz="UTC") + pd.Timedelta(minutes=40)).floor("min")
    pd.DataFrame([{"time": soon.isoformat(), "currency": "USD", "impact": "high", "title": "CPI m/m",
                   "forecast": "0.3%", "previous": "0.2%"},
                  {"time": (soon + pd.Timedelta(days=1)).isoformat(), "currency": "EUR", "impact": "low",
                   "title": "Trade Balance", "forecast": "", "previous": ""}]).to_csv(ws / "data" / "news_events.csv", index=False)
    evs = client.get("/api/news/events").json()["events"]
    cpi = next(e for e in evs if e["title"] == "CPI m/m")
    assert cpi["forecast"] == "0.3%" and cpi["info"]["name"].startswith("Consumer price") and cpi["note"] == ""
    assert any(e["impact"] == "low" for e in evs)
    body = {"time": cpi["time"], "currency": "USD", "title": "CPI m/m", "note": "Close gold trades first", "alert_minutes": 30}
    assert client.put(f"/api/news/events/{cpi['key']}", json={**body, "title": "Other"}, headers=H).status_code == 422
    assert client.put(f"/api/news/events/{cpi['key']}", json=body, headers=H).status_code == 200
    cpi = next(e for e in client.get("/api/news/events").json()["events"] if e["key"] == cpi["key"])
    assert cpi["note"] == "Close gold trades first" and cpi["alert_minutes"] == 30

    store = StateStore(ws / "data" / "state.db")
    sent = []

    class N:
        name = "t"
        def send(self, m):
            sent.append(m)
    assert news.send_reminders(store, [N()], now=soon - pd.Timedelta(minutes=35)) == []  # too early
    assert news.send_reminders(store, [N()], now=soon - pd.Timedelta(minutes=29)) == [cpi["key"]]
    assert "in 29 minutes" in sent[0].title and "Close gold trades first" in sent[0].body
    assert news.send_reminders(store, [N()], now=soon - pd.Timedelta(minutes=20)) == []  # only once
    store.close()
    # clearing note and reminder removes it
    client.put(f"/api/news/events/{cpi['key']}", json={**body, "note": "", "alert_minutes": None}, headers=H)
    assert next(e for e in client.get("/api/news/events").json()["events"] if e["key"] == cpi["key"])["alert_minutes"] is None


def test_export_import_strategy(client):
    sid = client.post("/api/strategies", json={"data": {**CSV_STRATEGY, "template": False}}, headers=H).json()["id"]
    r = client.get(f"/api/strategies/{sid}/export")
    assert r.headers["content-disposition"].startswith("attachment") and "template: true" in r.text
    new = client.post("/api/strategies/import", json={"yaml": r.text}, headers=H)
    assert new.status_code == 201 and new.json()["id"] != sid
    got = client.get(f"/api/strategies/{new.json()['id']}").json()["data"]
    assert got["template"] is True and got["entry"] == CSV_STRATEGY["entry"]
    assert client.post("/api/strategies/import", json={"yaml": "just: text"}, headers=H).status_code == 422
    assert client.post("/api/strategies/import", json={"yaml": "a: [unclosed"}, headers=H).status_code == 422
    link = client.post("/api/strategies/import", json={"data": CSV_STRATEGY}, headers=H)
    assert link.status_code == 201


def test_backup_and_restore(client, ws):
    import io
    import zipfile
    sid = client.post("/api/strategies", json={"data": CSV_STRATEGY}, headers=H).json()["id"]
    client.post("/api/journal", json={"market": "gold", "direction": "long", "entry_time": "2025-01-01T10:00",
                                      "entry_price": 2600, "size": 1}, headers=H)
    blob = client.get("/api/backup").content
    names = zipfile.ZipFile(io.BytesIO(blob)).namelist()
    assert {"config.yaml", "data/state.db", f"strategies/{sid}.yaml", "trading-alerts-backup.txt"} <= set(names)
    # change things, then restore
    client.delete(f"/api/strategies/{sid}", headers=H)
    jid = client.get("/api/journal").json()["entries"][0]["id"]
    client.delete(f"/api/journal/{jid}", headers=H)
    r = client.post("/api/restore", content=blob, headers={**H, "Content-Type": "application/zip"})
    assert r.status_code == 200, r.text
    assert any(x["id"] == sid for x in client.get("/api/strategies").json()["strategies"])
    assert len(client.get("/api/journal").json()["entries"]) == 1
    assert list((ws / "data" / "backups").glob("before-restore-*.zip"))
    # refuses anything else
    bad = io.BytesIO()
    with zipfile.ZipFile(bad, "w") as z:
        z.writestr("trading-alerts-backup.txt", "x")
        z.writestr("../evil.yaml", "x")
    assert client.post("/api/restore", content=bad.getvalue(), headers=H).status_code == 422
    assert client.post("/api/restore", content=b"not a zip", headers=H).status_code == 422


def test_weekly_report_and_notifications(client, ws):
    from tradealerts import weekly as report
    sid = client.post("/api/strategies", json={"data": CSV_STRATEGY}, headers=H).json()["id"]
    client.post(f"/api/strategies/{sid}/forward-test/start", headers=H)
    r = client.get("/api/report").json()
    assert r["tests"][0]["name"] == CSV_STRATEGY["name"] and r["tests"][0]["active"]
    page = client.get("/api/report.html")
    assert page.headers["content-type"].startswith("text/html") and "Your trading week" in page.text
    assert "Forward tests" in report.as_text(r) or "No forward tests" in report.as_text(r)
    sent = client.post("/api/report/send", headers=H).json()
    assert sent["errors"] == [] and "console" in sent["sent_to"]
    # due once a week at the chosen local time
    marker = ws / "sent"
    sun_7pm = pd.Timestamp("2026-11-08 19:00")
    assert report.due({"weekly": True, "day": "sun", "hour": 18}, marker, sun_7pm)
    assert not report.due({"weekly": True, "day": "sun", "hour": 20}, marker, sun_7pm)
    assert not report.due({"weekly": False, "day": "sun", "hour": 18}, marker, sun_7pm)
    marker.write_text("2026-11-08")
    assert not report.due({"weekly": True, "day": "sun", "hour": 18}, marker, sun_7pm)
    assert isinstance(client.get("/api/notifications").json()["items"], list)


def test_email_settings_keep_password(client, ws):
    s = client.get("/api/settings").json()
    s["notifiers"]["email"].update(enabled=True, host="smtp.example.com", port="587", to="me@example.com",
                                   username="me@example.com", password="app-pass")
    assert client.put("/api/settings", json=s, headers=H).status_code == 200
    back = client.get("/api/settings").json()["notifiers"]["email"]
    assert back["password"] == "" and back["password_set"] is True
    back["host"] = "smtp2.example.com"
    s["notifiers"]["email"] = back
    client.put("/api/settings", json=s, headers=H)
    saved = yaml.safe_load((ws / "config.yaml").read_text())
    email = next(n for n in saved["notifiers"] if n["type"] == "email")
    assert email["password"] == "app-pass" and email["host"] == "smtp2.example.com"


# A recipe strategy as v0.1 saved it: the market's Yahoo symbol instead of its key.
V01_STRATEGY = """\
name: Bounce from oversold on UK 100
template: true
recipe:
  id: rsi
  market: uk100
  settings: {level: 40, rsi_period: 6, trend_filter: false, stop: normal, target: '2', max_hold: 24,
             direction: long, timeframe: 1h, balance: 1000, risk_pct: 3, currency: GBP}
market: {symbol: ^FTSE, label: UK 100, provider: yahoo, timeframe: 1h}
direction: long
entry:
  all:
  - left: {indicator: rsi, period: 6, source: close}
    op: crosses_above
    right: 40
exit:
  stop: {atr: 2, atr_period: 14}
  target: {r_multiple: 2}
  max_bars: 24
risk: {account_size: 1000, risk_per_trade_pct: 3, point_value: 1, size_step: 0.01, min_size: 0.01, currency: GBP}
costs: {spread: 1}
"""


def test_v01_recipe_market_is_upgraded():
    from tradealerts.recipes import still_matches_recipe
    from tradealerts.strategy import upgrade_legacy_market

    data = yaml.safe_load(V01_STRATEGY)
    assert upgrade_legacy_market(data) is True
    assert data["market"] == {"symbol": "uk100", "label": "UK 100", "provider": "auto", "timeframe": "1h"}
    assert still_matches_recipe(data)
    assert upgrade_legacy_market(data) is False  # already done
    # hand-edited markets are left alone
    custom = yaml.safe_load(V01_STRATEGY)
    custom["market"]["symbol"] = "^GSPC"
    assert upgrade_legacy_market(custom) is False
    no_recipe = yaml.safe_load(V01_STRATEGY)
    del no_recipe["recipe"]
    assert upgrade_legacy_market(no_recipe) is False


def test_v01_strategy_files_are_rewritten_on_startup(ws):
    from tradealerts.strategy import load_strategy

    f = ws / "strategies" / "bounce-from-oversold-on-uk-100.yaml"
    f.write_text(V01_STRATEGY)
    client = TestClient(create_app(ws / "config.yaml"))
    s = load_strategy(f)
    assert (s.market.provider, s.market.symbol) == ("auto", "uk100")
    assert s.spread == 1  # the strategy's own setting is kept
    assert s.funding_pct == 6.0  # blank before, so the market's default applies
    item = next(x for x in client.get("/api/strategies").json()["strategies"] if x["id"] == "bounce-from-oversold-on-uk-100")
    assert item["data"]["market"]["provider"] == "auto"


def test_v01_strategy_import_is_upgraded(client):
    new = client.post("/api/strategies/import", json={"yaml": V01_STRATEGY}, headers=H)
    assert new.status_code == 201
    got = client.get(f"/api/strategies/{new.json()['id']}").json()["data"]
    assert (got["market"]["provider"], got["market"]["symbol"]) == ("auto", "uk100")



def test_v01_strategy_in_a_backup_is_upgraded_on_restore(client, ws):
    import io
    import zipfile
    blob = io.BytesIO()
    with zipfile.ZipFile(blob, "w") as z:
        z.writestr("trading-alerts-backup.txt", "Trading Alerts backup")
        z.writestr("strategies/old-uk100.yaml", V01_STRATEGY)
    r = client.post("/api/restore", content=blob.getvalue(), headers={**H, "Content-Type": "application/zip"})
    assert r.status_code == 200, r.text
    got = client.get("/api/strategies/old-uk100").json()["data"]
    assert (got["market"]["provider"], got["market"]["symbol"]) == ("auto", "uk100")
