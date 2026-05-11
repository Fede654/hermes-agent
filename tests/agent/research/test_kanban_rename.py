"""Pin the May 9 Lattice → Kanban rename.

The public surface for tracked research runs is `kanban_task_id`. The
old `lattice_task_id` parameter was removed when ProgressSink/KanbanSink
replaced the inline Lattice CLI shell-out. Tests in this file regress
if the rename gets undone or partially reverted.
"""

import inspect


def _params(callable_obj):
    return set(inspect.signature(callable_obj).parameters.keys())


def test_run_research_signature_uses_kanban_task_id():
    from tools.research_tool import run_research
    params = _params(run_research)
    assert "kanban_task_id" in params, (
        "run_research must accept kanban_task_id (the post-Lattice public name)"
    )
    assert "lattice_task_id" not in params, (
        "lattice_task_id was removed in the ProgressSink/KanbanSink refactor; "
        "its return indicates an incomplete or reverted rename"
    )


def test_research_job_signature_uses_kanban_task_id():
    from tools.research_job_tool import RESEARCH_JOB_SCHEMA
    props = RESEARCH_JOB_SCHEMA["parameters"]["properties"]
    assert "kanban_task_id" in props
    assert "lattice_task_id" not in props


def test_research_job_handler_signature_uses_kanban_task_id():
    """The _action_start handler must thread kanban_task_id through to spec."""
    import tools.research_job_tool as mod
    src = inspect.getsource(mod._action_start)
    assert "kanban_task_id" in src
    assert "lattice_task_id" not in src
