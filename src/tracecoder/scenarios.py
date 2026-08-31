"""Small product-scenario prompts layered on the same TraceCoder agent core."""

from __future__ import annotations

from typing import Literal

ScenarioName = Literal["general", "repair", "generate"]

_REPAIR_GUIDANCE = """Scenario: coursework project repair.
Inspect the existing project before editing. Reproduce or identify the defect, make the smallest focused fix,
and run an appropriate verification command. Preserve the existing project structure unless the task requires a
structural change. Report any limitation using runtime evidence rather than claiming unobserved success.
"""

_GENERATION_GUIDANCE = """Scenario: small coursework project generation.
Create a small runnable project in the relative target directory named by the user. If no target is named, use
course_project. Keep the result to roughly 5-10 purposeful files including a README, source code, and tests.
Use create_directory for new directories, prefer Python standard library plus unittest unless another language is
explicitly requested, and run the generated tests with purpose='verify'. This is a bounded demo, not a large-project
generator.
"""


def apply_scenario(task: str, scenario: ScenarioName) -> str:
    """Add deterministic workflow guidance without creating a second agent engine."""

    if scenario == "general":
        return task
    if scenario == "repair":
        guidance = _REPAIR_GUIDANCE
    elif scenario == "generate":
        guidance = _GENERATION_GUIDANCE
    else:
        raise ValueError(f"unknown scenario: {scenario}")
    return f"{guidance.strip()}\n\nUser task:\n{task}"
