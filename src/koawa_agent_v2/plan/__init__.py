"""D24 W1: plan board 与 update_plan 工具核心。

计划是任务分解事实的权威投影，本身不携带任何授权语义：本包没有任何路径
能放行动作、解除审批或修改 run 状态（D24 §W1 治理合同）。
"""

from .board import PlanBoard, PlanError, PlanItem, PlanLimits
from .tools import PlanToolRegistry, UpdatePlanArguments, plan_tool_spec

__all__ = [
    "PlanBoard",
    "PlanError",
    "PlanItem",
    "PlanLimits",
    "PlanToolRegistry",
    "UpdatePlanArguments",
    "plan_tool_spec",
]
