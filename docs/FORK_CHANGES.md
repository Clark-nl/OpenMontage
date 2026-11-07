# Changes in this fork

This is a modified version of [calesthio/OpenMontage](https://github.com/calesthio/OpenMontage),
forked at commit `d21f5d4` on 2026-11-07 and distributed under the GNU AGPLv3.

## 2026-11-07

- Added `tools/budget_gate.py`: every paid tool call is reserved against a ledger
  and refused when it does not fit the budget. `config.yaml` now uses `cap` mode.
- `math_animate` refuses to run unless `OPENMONTAGE_ALLOW_CODE_EXEC=1` is set.
- `animated-explainer` no longer offers `math_animate` or custom scripts.
- Added `requirements.lock` with resolved Python dependency versions.
