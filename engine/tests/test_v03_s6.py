"""0.3 session 6: visual rule editor upgrades (negated leaves, lossless round-trip, YAML + signal count endpoints)."""
import json
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from tradealerts import rules
from tradealerts.sample import synthetic_bars
from tradealerts.strategy import dump_strategy
from tradealerts.web import create_app

H = {"X-Requested-With": "tradealerts"}
STATIC = Path(__file__).resolve().parents[1] / "src" / "tradealerts" / "static"

NEGATED_LEAF = {"not": {"left": {"indicator": "rsi", "period": 14}, "op": ">", "right": 70}}
CUSTOM_NODE = {"not": {"not": {"left": "close", "op": ">", "right": "open"}}}  # valid for rules.py, no builder inputs
ENTRY = {"all": [
    {"left": {"indicator": "rsi", "period": 14}, "op": "crosses_above", "right": 35},
    NEGATED_LEAF,
    {"any": [{"weekdays": ["mon", "tue"]}, CUSTOM_NODE]},
]}

STRATEGY = {
    "name": "Session 6",
    "template": True,
    "market": {"symbol": "data/bars.csv", "label": "Test", "provider": "csv", "timeframe": "1h"},
    "direction": "long",
    "entry": ENTRY,
    "exit": {"stop": {"atr": 2, "atr_period": 14}, "target": {"r_multiple": 2}, "max_bars": 24},
    "risk": {"account_size": 1000, "risk_per_trade_pct": 1, "currency": "GBP"},
}


@pytest.fixture
def client(tmp_path):
    (tmp_path / "strategies").mkdir()
    (tmp_path / "data").mkdir()
    synthetic_bars(days=90, freq="1h", seed=4).to_csv(tmp_path / "data" / "bars.csv")
    (tmp_path / "config.yaml").write_text("poll_minutes: 15\nnotifiers:\n  - type: console\n")
    return TestClient(create_app(tmp_path / "config.yaml"))


def test_rule_tree_round_trip_is_lossless(client):
    """Rule tree -> editor JSON (the server's view of a draft) -> YAML -> rule tree, unchanged."""
    rules.compile_condition(ENTRY)  # negated leaf and the custom node are valid rules
    r = client.post("/api/yaml", json={"data": STRATEGY}, headers=H)
    assert r.status_code == 200
    back = yaml.safe_load(r.json()["yaml"])
    assert back["entry"] == ENTRY
    assert yaml.safe_load(dump_strategy(json.loads(json.dumps(STRATEGY))))["entry"] == ENTRY
    r = client.post("/api/yaml/parse", json={"yaml": r.json()["yaml"]}, headers=H)
    assert r.status_code == 200 and r.json()["ok"] is True, r.json()
    assert r.json()["data"]["entry"] == ENTRY


def test_yaml_parse_reports_mistakes(client):
    bad = client.post("/api/yaml/parse", json={"yaml": "name: [oops"}, headers=H).json()
    assert bad["ok"] is False and "YAML" in bad["error"]
    broken = dict(STRATEGY, entry={"all": [{"left": "close", "op": "<<<", "right": 1}]})
    r = client.post("/api/yaml/parse", json={"yaml": yaml.safe_dump(broken)}, headers=H).json()
    assert r["ok"] is False and r["error"] and r["data"]["name"] == "Session 6"


def test_signal_count_endpoint(client):
    r = client.post("/api/signals/count", json={"data": STRATEGY}, headers=H)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["candles"] > 0 and 0 <= body["signals"] <= body["candles"]
    assert body["start"] and body["end"]
    always = dict(STRATEGY, entry={"all": [{"left": "close", "op": ">", "right": -1}]})
    assert client.post("/api/signals/count", json={"data": always}, headers=H).json()["signals"] == body["candles"]
    never = dict(STRATEGY, entry={"all": [{"left": "close", "op": "<", "right": -1}]})
    assert client.post("/api/signals/count", json={"data": never}, headers=H).json()["signals"] == 0


JS_CHECK = r"""
const vm = require("vm"), fs = require("fs");
const ctx = { console, document: { getElementById: () => null, body: { appendChild() {} } }, app: {} };
vm.createContext(ctx);
for (const f of ["util.js", "describe.js", "editor.js"]) vm.runInContext(fs.readFileSync(process.argv[1] + "/" + f, "utf8"), ctx);
vm.runInContext("app.meta = " + JSON.stringify({ indicators: [{ name: "rsi", label: "RSI", params: { period: 14 }, types: { period: "int" } }], fields: ["close"] }), ctx);
const [neg, custom, grp] = JSON.parse(process.argv[2]);
const text = (parts) => parts.map((p) => (typeof p === "string" ? p : (p && p.m) || "")).join("");
console.log(JSON.stringify({
  negKind: ctx.groupKind(neg), negLeaf: ctx.isNegatedLeaf(neg), negEditable: ctx.editableLeaf(neg),
  customLeaf: ctx.isNegatedLeaf(custom), customEditable: ctx.editableLeaf(custom),
  grpKind: ctx.groupKind(grp), negDesc: text(ctx.describeCondition(neg)), custDesc: text(ctx.describeCondition(custom)),
}));
"""


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_editor_js_handles_negated_leaves():
    out = subprocess.run(["node", "-e", JS_CHECK, "--", str(STATIC),
                          json.dumps([NEGATED_LEAF, CUSTOM_NODE, {"not": {"any": []}}])],
                         capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    got = json.loads(out.stdout)
    assert got["negKind"] is None and got["negLeaf"] is True and got["negEditable"] is True
    assert got["customEditable"] is False  # no builder inputs: kept as a read-only card
    assert got["grpKind"] == "none"
    assert got["negDesc"].startswith("it's not true that")
    assert "is above 70" in got["negDesc"]
    assert got["custDesc"].count("it's not true that") == 2
