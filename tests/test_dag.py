import pytest

from scrapewatch.config import ROOT

pytest.importorskip("airflow")


def test_dag_imports_and_is_wired_in_order():
    from airflow.dag_processing.dagbag import DagBag

    bag = DagBag(dag_folder=str(ROOT / "dags"))
    assert bag.import_errors == {}
    dag = bag.dags["scrapewatch"]
    assert {t.task_id for t in dag.tasks} == {"sites", "scrape", "monitor", "load_silver", "dbt_build", "dq_gate"}
    down = {t.task_id: t.downstream_task_ids for t in dag.tasks}
    assert down["sites"] == {"scrape"}
    assert down["scrape"] == {"monitor"}
    assert down["monitor"] == {"load_silver", "dq_gate"}
    assert down["load_silver"] == {"dbt_build", "dq_gate"}
    assert down["dbt_build"] == {"dq_gate"}
    assert dag.get_task("scrape").max_active_tis_per_dag == 1
