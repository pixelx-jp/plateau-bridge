"""Gate A / B / C orchestrators.

Each gate is independently runnable and produces a verifiable artifact set.
A failing Gate B does not block Gate A consumers; see docs/architecture.md.
"""

from plateau_bridge.pipeline.gate_a import run_gate_a
from plateau_bridge.pipeline.gate_b import run_gate_b
from plateau_bridge.pipeline.gate_c import run_gate_c

__all__ = ["run_gate_a", "run_gate_b", "run_gate_c"]
