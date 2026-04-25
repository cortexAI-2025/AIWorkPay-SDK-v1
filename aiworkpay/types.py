from typing import Any, Literal, Optional
from typing_extensions import TypedDict

TaskStatus = Literal["pending", "assigned", "completed", "failed"]
QualityLevel = Literal["low", "medium", "high"]


class CreateTaskInput(TypedDict, total=False):
    type: str
    instructions: str
    input_data: Any
    expected_output: str
    budget: float
    deadline: str
    quality_level: QualityLevel


class Task(TypedDict, total=False):
    id: str
    status: TaskStatus
    type: str
    instructions: str
    input_data: Any
    result: Any
    budget: float
    created_at: str


class ApproveResponse(TypedDict, total=False):
    task_id: str
    paid: bool
    amount: float
    message: str


class RejectResponse(TypedDict, total=False):
    task_id: str
    reason: Optional[str]
    message: str
