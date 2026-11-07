"""Hard budget cap around every paid tool call.

CostTracker on its own only works if the agent remembers to call it. This gate
wraps every BaseTool.execute(), so a paid call is reserved against the ledger
before it runs and refused when the budget cannot cover it.

Interactive approvals stay in the agent's checkpoint flow; this layer only
enforces the money limit. Selectors delegate to provider tools, so the outermost
call that reserved is the one that counts and nested calls pass through.

Env:
  OPENMONTAGE_COST_LOG  ledger path. Default: pipeline/cost_log.json. An existing
                        ledger keeps its own budget_total_usd, so give each
                        project or job its own file.
  OPENMONTAGE_BUDGET    "off" disables the gate (used by the test suite).
"""

from __future__ import annotations

import contextlib
import functools
import os
import threading
import time
from pathlib import Path
from typing import Any, Callable, Iterator

_state = threading.local()
_LOCK_TIMEOUT_SECONDS = 10
_LOCK_STALE_SECONDS = 30


def _enabled() -> bool:
    return os.environ.get("OPENMONTAGE_BUDGET", "").strip().lower() != "off"


def _ledger_path() -> Path:
    configured = os.environ.get("OPENMONTAGE_COST_LOG", "").strip()
    if configured:
        return Path(configured)
    return Path(__file__).resolve().parent.parent / "pipeline" / "cost_log.json"


@contextlib.contextmanager
def _locked(ledger: Path) -> Iterator[None]:
    """Serialize ledger updates across processes; agents run tools in parallel."""
    lock = ledger.with_suffix(".lock")
    lock.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + _LOCK_TIMEOUT_SECONDS
    while True:
        try:
            os.close(os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY))
            break
        except FileExistsError:
            with contextlib.suppress(OSError):
                if time.time() - lock.stat().st_mtime > _LOCK_STALE_SECONDS:
                    lock.unlink()
            if time.monotonic() > deadline:
                raise TimeoutError(f"Could not lock budget ledger {ledger}")
            time.sleep(0.05)
    try:
        yield
    finally:
        lock.unlink(missing_ok=True)


def _tracker(ledger: Path) -> Any:
    from lib.config_model import BudgetMode, OpenMontageConfig
    from tools.cost_tracker import CostTracker

    budget = OpenMontageConfig.load().budget
    return CostTracker(
        budget_total_usd=budget.total_usd,
        reserve_pct=budget.reserve_pct,
        single_action_approval_usd=float("inf"),
        require_approval_for_new_paid_tool=False,
        mode=BudgetMode.CAP,
        cost_log_path=ledger,
    )


def _settle(ledger: Path, entry_id: str, actual_usd: float, success: bool) -> None:
    # An unsettled entry stays reserved, which over-counts rather than
    # under-counts, so a ledger hiccup must not throw away a finished render.
    with contextlib.suppress(OSError, TimeoutError):
        with _locked(ledger):
            _tracker(ledger).reconcile(entry_id, actual_usd, success=success)


def _actual_cost(result: Any, estimated: float) -> float:
    if getattr(result, "from_cache", False):
        return 0.0
    reported = getattr(result, "cost_usd", None)
    has_report = isinstance(reported, (int, float)) and reported > 0
    if getattr(result, "success", False):
        # ToolResult.cost_usd defaults to 0.0, so 0 cannot mean "free" here.
        return float(reported) if has_report else estimated
    return float(reported) if has_report else 0.0


def enforce_budget(fn: Callable) -> Callable:
    """Wrap a tool's execute() so paid calls must fit the remaining budget."""
    if getattr(fn, "_budget_enforced", False):
        return fn

    @functools.wraps(fn)
    def wrapper(self, inputs: Any, *args: Any, **kwargs: Any):
        if not _enabled() or getattr(_state, "held", False):
            return fn(self, inputs, *args, **kwargs)

        from tools.base_tool import ToolResult
        from tools.cost_tracker import BudgetExceededError

        tool = getattr(self, "name", "") or self.__class__.__name__
        try:
            estimated = float(self.estimate_cost(inputs))
        except Exception as exc:
            # Fail closed: a price we cannot compute is a price we cannot cap.
            return ToolResult(
                success=False,
                error=f"Budget gate blocked {tool}: no cost estimate ({exc})",
            )
        if estimated <= 0:
            return fn(self, inputs, *args, **kwargs)

        ledger = _ledger_path()
        operation = str(inputs.get("operation", "execute")) if isinstance(inputs, dict) else "execute"
        try:
            with _locked(ledger):
                tracker = _tracker(ledger)
                entry_id = tracker.estimate(tool, operation, estimated)
                tracker.reserve(entry_id)
        except (BudgetExceededError, OSError, TimeoutError, ValueError) as exc:
            return ToolResult(
                success=False,
                error=(
                    f"Budget gate blocked {tool}: {exc}. Ledger: {ledger}. "
                    "Raise budget_total_usd there or set OPENMONTAGE_COST_LOG "
                    "to a new ledger."
                ),
            )

        _state.held = True
        try:
            result = fn(self, inputs, *args, **kwargs)
        except BaseException:
            # The provider may already have billed; keep the estimate on the books.
            _settle(ledger, entry_id, estimated, success=False)
            raise
        finally:
            _state.held = False
        _settle(
            ledger,
            entry_id,
            _actual_cost(result, estimated),
            success=bool(getattr(result, "success", False)),
        )
        return result

    wrapper._budget_enforced = True  # type: ignore[attr-defined]
    return wrapper
