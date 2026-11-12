"""Daily scrapewatch run: scrape (one mapped task per site config) -> monitor -> load silver -> dbt build -> DQ gate.

Tasks are thin wrappers over scrapewatch.pipeline, so the CLI and Airflow run identical code.
Adding a site is adding a YAML file: `sites()` lists configs/sites/ at run time and the scrape task maps over it.
"""

from datetime import datetime, timedelta

from airflow.sdk import dag, task

from scrapewatch import pipeline


@dag(
    schedule="@daily",
    start_date=datetime(2026, 11, 1),
    catchup=False,
    max_active_runs=1,
    default_args={"retries": 1, "retry_delay": timedelta(minutes=10)},
    tags=["scrapewatch"],
)
def scrapewatch():
    @task
    def sites() -> list[str]:
        return list(pipeline.configs())

    # Site problems (403, 5xx, broken selectors) never raise: they become monitor findings. A retry here only
    # covers infrastructure failures. One at a time, because two configs can share a host (quotes + quotes_js)
    # and the per-host rate limit lives in one process.
    @task(max_active_tis_per_dag=1, retries=2, retry_exponential_backoff=True)
    def scrape(site: str) -> dict:
        return pipeline.step_scrape(site)

    @task
    def monitor(metrics: dict) -> dict:
        return pipeline.step_monitor(metrics)

    @task
    def load_silver(monitored: list[dict]) -> list[dict]:
        return pipeline.step_silver()

    @task
    def dbt_build(silver_stats: list[dict]) -> dict:
        return pipeline.step_dbt("build")

    @task
    def dq_gate(monitored: list[dict], silver_stats: list[dict], dbt_result: dict) -> dict:
        return pipeline.step_gate(list(monitored), silver_stats, dbt_result)

    monitored = monitor.expand(metrics=scrape.expand(site=sites()))
    silver_stats = load_silver(monitored)
    dq_gate(monitored, silver_stats, dbt_build(silver_stats))


scrapewatch()
