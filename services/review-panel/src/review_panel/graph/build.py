"""Assemble and compile the panel StateGraph (ADR-0030 §3 amended by ADR-0031).

Topology: load_pr -> four reviewer nodes in parallel -> join -> reconcile ->
store_draft -> END. The terminal node PERSISTS the draft — no posting node
exists anywhere in the graph (asserted by tests). Checkpointing makes a killed
run resume instead of re-running completed reviewer nodes.
"""

from collections.abc import Sequence

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from review_panel.domain.findings import PANEL_LENSES
from review_panel.graph.nodes import (
    PanelDependencies,
    make_load_pr,
    make_reconcile,
    make_reviewer,
    make_store_draft,
)
from review_panel.graph.state import PanelState

PanelGraph = CompiledStateGraph[PanelState, None, PanelState, PanelState]

LOAD_NODE = "load_pr"
REVIEWER_NODES: tuple[str, ...] = tuple(f"review_{lens}" for lens in PANEL_LENSES)
RECONCILE_NODE = "reconcile"
STORE_NODE = "store_draft"

#: The complete node set — the dev-gate tests assert no other node ever appears.
PANEL_NODES: tuple[str, ...] = (LOAD_NODE, *REVIEWER_NODES, RECONCILE_NODE, STORE_NODE)


def build_panel_graph(
    deps: PanelDependencies,
    checkpointer: BaseCheckpointSaver[str] | None = None,
    *,
    interrupt_before: Sequence[str] = (),
) -> PanelGraph:
    builder: StateGraph[PanelState, None, PanelState, PanelState] = StateGraph(PanelState)
    builder.add_node(LOAD_NODE, make_load_pr(deps))
    for lens, node_name in zip(PANEL_LENSES, REVIEWER_NODES, strict=True):
        builder.add_node(node_name, make_reviewer(deps, lens))
    builder.add_node(RECONCILE_NODE, make_reconcile(deps))
    builder.add_node(STORE_NODE, make_store_draft(deps))

    builder.add_edge(START, LOAD_NODE)
    for node_name in REVIEWER_NODES:  # fan-out: one superstep, four parallel branches
        builder.add_edge(LOAD_NODE, node_name)
    builder.add_edge(list(REVIEWER_NODES), RECONCILE_NODE)
    builder.add_edge(RECONCILE_NODE, STORE_NODE)
    builder.add_edge(STORE_NODE, END)
    return builder.compile(
        checkpointer=checkpointer,
        interrupt_before=list(interrupt_before) or None,
    )
