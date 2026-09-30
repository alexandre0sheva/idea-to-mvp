from plan_helpers import make_plan, make_task

from idea_to_mvp.implementation.scheduler import blocked_tasks, dag_width, next_ready


def diamond():
    return make_plan(
        [
            make_task("T01"),
            make_task("T02", depends_on=["T01"]),
            make_task("T03", depends_on=["T01"]),
            make_task("T04", depends_on=["T02", "T03"]),
        ]
    )


def ids(tasks) -> list[str]:
    return [task.id for task in tasks]


def test_only_the_root_is_ready_at_the_start() -> None:
    assert ids(next_ready(diamond(), done=set(), running=set())) == ["T01"]


def test_the_two_branches_become_ready_together_only_after_the_root() -> None:
    plan = diamond()
    assert ids(next_ready(plan, done={"T01"}, running=set())) == ["T02", "T03"]
    assert ids(next_ready(plan, done=set(), running={"T01"})) == []  # T01 is still running: nothing else can start


def test_the_join_waits_for_both_branches() -> None:
    plan = diamond()
    assert ids(next_ready(plan, done={"T01", "T02"}, running={"T03"})) == []
    assert ids(next_ready(plan, done={"T01", "T02", "T03"}, running=set())) == ["T04"]


def test_running_and_done_tasks_are_never_offered_again() -> None:
    plan = diamond()
    assert ids(next_ready(plan, done={"T01"}, running={"T02"})) == ["T03"]
    assert ids(next_ready(plan, done={"T01", "T02", "T03", "T04"}, running=set())) == []


def test_ready_tasks_come_back_in_task_id_order() -> None:
    plan = make_plan([make_task("T03"), make_task("T01"), make_task("T02")])
    assert ids(next_ready(plan, done=set(), running=set())) == ["T01", "T02", "T03"]


def test_a_task_with_an_unknown_dependency_is_never_ready() -> None:
    plan = make_plan([make_task("T01", depends_on=["T09"])])
    assert next_ready(plan, done=set(), running=set()) == []


# ---------------------------------------------------------- tasks behind a dead task


def test_tasks_behind_a_failed_task_are_blocked_transitively() -> None:
    plan = make_plan(
        [make_task("T01"), make_task("T02", depends_on=["T01"]), make_task("T03", depends_on=["T02"]), make_task("T04")]
    )
    blocked = blocked_tasks(plan, dead={"T01"}, finished={"T01"})
    assert blocked == {"T02": ["T01"], "T03": ["T02"]}  # T04 is independent and unaffected


def test_finished_tasks_are_never_reported_as_blocked() -> None:
    plan = diamond()
    assert blocked_tasks(plan, dead={"T02"}, finished={"T01", "T02", "T04"}) == {}  # T04 already has a result


def test_a_join_is_blocked_by_its_dead_branch_only() -> None:
    assert blocked_tasks(diamond(), dead={"T03"}, finished={"T01", "T03"}) == {"T04": ["T03"]}


# ------------------------------------------------------------------- dag width


def test_dag_width_is_the_largest_dependency_layer() -> None:
    assert dag_width(diamond()) == 2
    assert dag_width(make_plan([make_task(f"T0{i}") for i in range(1, 6)])) == 5
    chain = make_plan([make_task("T01"), make_task("T02", depends_on=["T01"]), make_task("T03", depends_on=["T02"])])
    assert dag_width(chain) == 1


def test_dag_width_of_an_empty_or_broken_plan_is_zero() -> None:
    assert dag_width(make_plan([])) == 0
    assert dag_width(make_plan([make_task("T01", depends_on=["T02"]), make_task("T02", depends_on=["T01"])])) == 0
