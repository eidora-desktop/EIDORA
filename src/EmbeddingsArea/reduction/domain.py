"""Plain reduction settings and results."""
from dataclasses import dataclass, field


@dataclass(frozen=True)
class ReductionRequest:
    method: str = "pca"
    dimensions: int = 48
    replace_original: bool = False
    label: str = ""


@dataclass(frozen=True)
class ReductionResult:
    run_id: str
    variance_retained: float | None
    dimensions: int
    warning: str = ""
    recipe: dict = field(default_factory=dict)
