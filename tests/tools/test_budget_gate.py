"""The hard budget cap must hold even when the agent never calls CostTracker."""

import json
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from tools.base_tool import BaseTool, ToolResult  # noqa: E402
from tools.provider_pricing import PriceQuoteRequired  # noqa: E402


class Paid(BaseTool):
    name = "paid"
    calls = 0
    cost = 6.0
    reported = 0.0

    def estimate_cost(self, inputs):
        return self.cost

    def execute(self, inputs):
        type(self).calls += 1
        return ToolResult(success=True, cost_usd=self.reported)


class Unpriced(Paid):
    name = "unpriced"

    def estimate_cost(self, inputs):
        raise PriceQuoteRequired("depends on the account")

    def execute(self, inputs):
        raise AssertionError("must not run without a price")


class Boom(Paid):
    name = "boom"

    def execute(self, inputs):
        raise RuntimeError("provider died after billing")


class Selector(BaseTool):
    name = "selector"
    cost = 6.0

    def estimate_cost(self, inputs):
        return self.cost

    def execute(self, inputs):
        return Paid().execute(inputs)


class Free(BaseTool):
    name = "free"

    def execute(self, inputs):
        return ToolResult(success=True)


@pytest.fixture
def ledger(tmp_path, monkeypatch):
    path = tmp_path / "cost_log.json"
    monkeypatch.delenv("OPENMONTAGE_BUDGET", raising=False)
    monkeypatch.setenv("OPENMONTAGE_COST_LOG", str(path))
    monkeypatch.setattr(Paid, "calls", 0)
    return path


def spent(path):
    return json.loads(path.read_text())["budget_spent_usd"]


def test_refuses_call_over_budget(ledger):
    assert Paid().execute({}).success  # 6 of the 9 usable ($10 minus 10% holdback)
    blocked = Paid().execute({})
    assert blocked.success is False
    assert "Budget gate blocked" in blocked.error
    assert Paid.calls == 1
    assert spent(ledger) == 6.0


def test_budget_survives_across_tool_instances_and_processes(ledger):
    Paid().execute({})
    ledger_data = json.loads(ledger.read_text())
    assert ledger_data["entries"][0]["status"] == "completed"
    assert ledger_data["budget_reserved_usd"] == 0


def test_reported_cost_replaces_estimate(ledger, monkeypatch):
    monkeypatch.setattr(Paid, "reported", 1.25)
    Paid().execute({})
    assert spent(ledger) == 1.25


def test_selector_and_provider_are_charged_once(ledger):
    assert Selector().execute({}).success
    assert Paid.calls == 1
    assert len(json.loads(ledger.read_text())["entries"]) == 1


def test_selector_without_estimate_still_gates_provider(ledger, monkeypatch):
    monkeypatch.setattr(Selector, "cost", 0.0)
    monkeypatch.setattr(Paid, "cost", 20.0)
    assert Selector().execute({}).success is False
    assert Paid.calls == 0


def test_unknown_price_fails_closed(ledger):
    result = Unpriced().execute({})
    assert result.success is False
    assert "no cost estimate" in result.error


def test_crash_keeps_estimate_on_the_books(ledger):
    with pytest.raises(RuntimeError):
        Boom().execute({})
    assert spent(ledger) == 6.0


def test_corrupt_ledger_fails_closed(ledger):
    ledger.write_text("{not json")
    assert Paid().execute({}).success is False
    assert Paid.calls == 0


def test_free_tools_never_touch_the_ledger(ledger):
    assert Free().execute({}).success
    assert not ledger.exists()


def test_off_switch(ledger, monkeypatch):
    monkeypatch.setenv("OPENMONTAGE_BUDGET", "off")
    monkeypatch.setattr(Paid, "cost", 999.0)
    assert Paid().execute({}).success
