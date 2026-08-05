# Data Platform: Multi-Service Orchestration with Dagster

A local-first data platform that runs **Kafka, Spark, Postgres, ClickHouse, and Dagster** in a single `make` command. Built as a reference implementation for multi-team data orchestration: not a single pipeline, but a platform where multiple teams deploy independently, validate each other's outputs at boundaries, and react to data as it arrives.

```
make
# → spins up 9 containerized services (10 with the NL-to-SQL profile)
# → opens Dagster UI at http://localhost:3000
```

## Contents

- [What this runs](#what-this-runs)
- [Quick start](#quick-start)
- [Architecture](#architecture)
- [Asset lineage](#asset-lineage)
- [Jobs](#jobs)
- [Repository structure](#repository-structure)
- [Environment configuration](#environment-configuration)
- [Production mapping](#production-mapping)
- [Usage notice](#usage-notice)

**Deep dives:**

| Document | What is in it |
|---|---|
| [Architecture](docs/architecture.md) | The three planes, key guarantees, asset lineage, execution results, validation gates, the dbt layer |
| [Streaming pipeline](docs/streaming.md) | Jetstream to ClickHouse, measured throughput, outage recovery, CDC semantics, whether this needs Spark |
| [When the purge took the warehouse down](docs/incident-2026-08-03.md) | An 80-minute ClickHouse outage, its three compounding causes, and the fix |
| [Design decisions](docs/design-decisions.md) | Watermarks, the Parquet landing zone, config shape, and the limits of each |
| [Conversational analytics](docs/nl2sql.md) | The NL-to-SQL service, its guardrails, and why table grain beats prompt engineering |
| [AWS deployment](deployment/AWS_DEPLOY.md) | EKS, RDS, S3, teardown that leaves nothing billing |
| [dbt models](code_locations/etl_pipeline/dbt/README.md) | The four models, tests, freshness policies, automation |

---

## What this runs

| Service | Role |
|---|---|
| **Kafka** | Message broker buffering the Bluesky firehose |
| **Kafka Producer** | Subscribes to Bluesky's Jetstream firehose and publishes to Kafka |
| **Spark Consumer** | Reads from Kafka via Structured Streaming, writes to Postgres |
| **Postgres** | Operational store: the streaming change log plus Dagster run metadata |
| **ClickHouse** | Analytical warehouse: one table per asset, shared across code locations |
| **Dagster Webserver** | UI and API layer (control plane) |
| **Dagster Daemon** | Scheduling, sensors, and run queue (control plane) |
| **ETL Code Location** | Team-owned pipeline: pulls, validates, cleans, and loads data |
| **ML Code Location** | Team-owned pipeline: validates ETL output schema, then runs downstream modeling |
| **Analytics API** | NL-to-SQL interface over the ClickHouse warehouse (optional, `dev-nl2sql` profile) |

All images are pre-built and published to Docker Hub. No local builds required.

---

## Quick start

**Prerequisites:** Docker Desktop, Git, macOS or Linux

```bash
git clone https://github.com/ajohnson114/data_platform.git
cd data_platform
make
```

Open the Dagster UI at `http://localhost:3000`.

To include the NL-to-SQL analytics interface:

```bash
make dev-nl2sql
```

That starts the full platform plus the analytics service. Open the query interface at `http://localhost:7860`, select your LLM provider, choose a model, and paste your API key into the UI. No environment variables required.

---

## Architecture

Three planes that never mix responsibilities:

- **Control plane** (Dagster webserver and daemon): scheduling, dependency resolution, run tracking, observability. It never executes business logic.
- **Execution plane** (code locations): each team owns its assets, checks and compute, and deploys independently via its own gRPC code server.
- **Data plane** (Postgres and ClickHouse): Postgres is the operational store, ClickHouse the analytical warehouse. Assets write to ClickHouse through a shared resource, one table per asset, so every team reads the same warehouse without coupling to another team's code.

```text
Jetstream  → Kafka Producer → Kafka → Spark Structured Streaming → Postgres.bsky_records
                                                                        │
                          Dagster sensor → bsky_records_snapshot ───────┤
                                                                        ├→ ClickHouse ─┬→ NL-to-SQL
generated → clean → save_data_to_postgres_db → Postgres.etl_table       │  (analytics)  └→ ml_pipeline
                                     └→ etl_table_snapshot ─────────────┘
```

![System Design](docs/sys_design_with_streaming.png)

### Key guarantees

**Team isolation.** Each code location is a separate container with separate dependencies. A crash or import error in one team's code cannot stop another team's pipelines from running.

**Validation-gated execution.** Blocking asset checks stop Dagster executing any downstream asset when an upstream check fails. Bad data stops at the boundary, not silently downstream.

**Clear ownership.** Every asset and every check has exactly one owning team. No shared mutable state, no hidden coupling across boundaries.

The reasoning behind each plane, the second-hop load, and where blocking is the *wrong* answer for a check is in [docs/architecture.md](docs/architecture.md).

---

## Asset lineage

The Dagster UI renders the full dependency graph across every asset group and both code locations:

![Global Asset Lineage](docs/asset_lineage_global.png)

Cross-team dependencies are first-class. `save_data_to_postgres_db` (ETL team) depends on `clean_data` from its own group and on `prepare_postgres_tables` from `db_setup`, and the UI draws that contract across the group boundary rather than hiding it inside a job. Every asset also carries the technology it touches (Postgres, Parquet, ClickHouse, dbt, Scikit Learn), which is what makes the graph readable as a system rather than as a list of Python functions.

That graph is live state after `etl_job`, `ml_pipeline_job` and `failing_job` have run with the streaming sensor going. Green is materialized, red in `failing_pipeline` is a deliberate demonstration of the gate, and the check counts are the interesting part. [Execution results](docs/architecture.md#execution-results) walks through what each count asserts.

---

## Jobs

| Job | What it does |
|---|---|
| `etl_job` | Creates database tables, loads mock data into Postgres, snapshots `etl_table` into the ClickHouse warehouse |
| `ml_pipeline_job` | Reads the ETL team's snapshot out of the warehouse, then trains and registers a model. Fails intentionally if prerequisites are missing |
| `streaming_ingest_job` | Triggered by `bsky_record_sensor`. Lands new Bluesky change events from Postgres as Parquet, then folds them into current state in ClickHouse |
| `dbt_job` | Builds `stg_bsky_records` and the three marts. Runs itself every 5 minutes via `dbt_rebuild_sensor` |
| `purge_job` | Physically removes retracted records from the warehouse and drops landed Parquet past the retention horizon. Daily at `17 3 * * *` |
| `rebuild_warehouse_from_landing` | Manual. Replays the Parquet landing zone into an empty warehouse, oldest id range first |

A few details that are easy to get wrong:

- `dbt_rebuild_sensor` evaluates an `AutomationCondition` rather than a schedule, so a rebuild is skipped while the previous one is still running.
- `purge_deleted_records` keys on `argMax(is_deleted, event_time_us)` rather than "has ever been tombstoned", so a record deleted and later re-created under the same key is spared. Its decision is global and only the `DELETE` is partition-scoped, which is a correctness requirement rather than an optimisation. See [the incident writeup](docs/incident-2026-08-03.md).
- `purge_landing_archive` never deletes the newest file of a dataset, whatever its age, because the extract's bookmark is read off that listing.
- 03:17 rather than 03:00 because every `0 H * * *` cron shares a tick with the marts' `*/5` rebuild by construction.
- `rebuild_warehouse_from_landing` reaches back as far as the 7-day landing retention horizon and no further. It takes an optional `from_id` to replay only a recent window.

Some failures are intentional and part of the demo.

---

## Repository structure

```text
code_locations/
  etl_pipeline/          # ETL team: assets, checks, jobs, sensors, resources
    dbt/                 # dbt models over the warehouse (see its own README)
  basic_ml_pipeline/     # ML team: assets, cross-team schema checks, jobs
  shared/                # Shared resources (ClickHouse warehouse, IO manager selection, DB client)
kafka_producer/          # Standalone service: Jetstream firehose → Kafka
spark_consumer/          # Standalone service: Kafka → Spark → Postgres
services/
  analytics_api/         # NL-to-SQL service: FastAPI + Gradio UI (optional)
deployment/
  docker-compose.yaml    # Local dev compose (builds from source)
  dockerfiles/           # All Dockerfiles
  clickhouse/            # Warehouse users: dagster (read/write), analytics_ro (readonly)
  k8s/                   # EKS manifests for the aws environment
  terraform/             # EKS, RDS, VPC, S3 infrastructure (Kafka is in k8s/)
  workspace.yaml         # Dagster code location registry
  dagster.yaml           # Dagster instance config
docker-compose.yaml      # Production compose (pre-built images)
Makefile                 # One-command startup
```

Each folder under `code_locations/` is a team deployment unit. `kafka_producer/` and `spark_consumer/` are standalone applications rather than Dagster code locations, and `services/` holds optional platform-level services. Teams can be added without modifying existing teams.

---

## Environment configuration

Each code location manages its own environment-scoped config and secrets, loaded at runtime from the `ENV` variable. The Makefile sets `dev`, `uat` and `prod` locally; the Kubernetes manifests set `aws` on EKS.

```text
code_locations/etl_pipeline/config/
  config/
    config.dev.yaml    # DB connection details, SQL, IO manager, asset check params
    config.uat.yaml
    config.prod.yaml
    config.aws.yaml
  secrets/
    secrets.dev.yaml   # DB credentials
    secrets.uat.yaml
    secrets.prod.yaml
    secrets.aws.yaml
```

`basic_ml_pipeline/config/` has the same structure.

Config files hold non-sensitive runtime parameters: hostnames, table names, SQL statements. Secrets files hold credentials. The split separates config management from secret management, and in the `aws` environment it is the delivery mechanism rather than an analogy. `deployment/k8s/12-etl-config.yaml` ships `config.aws.yaml` as a ConfigMap and `13-etl-secrets.yaml` ships `secrets.aws.yaml` as a Secret, each `subPath`-mounted over the matching file in the image's baked config tree. The code location reads the same two paths it always reads, and only their source changes.

Running `make`, `make uat`, or `make prod` injects the correct `ENV` value into each container, which loads the corresponding file pair at startup. On EKS the manifests set `ENV: aws` and `aws_up.sh` renders the real RDS and ClickHouse values into the ConfigMap and Secret before applying them.

---

## Production mapping

| Local | Production |
|---|---|
| Docker Compose | Kubernetes / Helm |
| Local executor | K8sJobExecutor / Celery |
| Single-node ClickHouse | Managed ClickHouse Cloud / Snowflake / BigQuery |
| Local Postgres | Managed cloud SQL (RDS, Cloud SQL) |
| Makefile | CI/CD pipelines |
| Kafka (single node) | Managed Kafka (MSK, Confluent) |
| Spark (local) | EMR / Dataproc / Databricks |
| Jetstream firehose consumer | Debezium CDC connectors, or the vendor's own change stream |
| NL-to-SQL service | Integrated analytics product layer |
| Config YAML files | Kubernetes ConfigMaps |
| Secrets YAML files | Kubernetes Secrets / Vault |

The architecture is designed so production hardening can be added without changing core abstractions. The `aws` environment already walks part of that path: the same images run on EKS against RDS, with ClickHouse and Kafka as StatefulSets and S3 behind the IO manager. See [`deployment/AWS_DEPLOY.md`](deployment/AWS_DEPLOY.md).

Kafka is deliberately *not* MSK there, which is the one row of this table the cloud deployment does not yet buy. One topic with one partition gains nothing from a replicated broker cluster, and the single-writer property it implies is load-bearing downstream. The reasoning, and what it costs in durability, is in [Why Kafka is not MSK](deployment/AWS_DEPLOY.md#why-kafka-is-not-msk).

---

## Usage notice

This repository is a work sample. It is MIT licensed, so clone it, run it, take ideas from it.

**A note on the data.** The streaming path consumes Bluesky's public Jetstream firehose, which carries real posts by real people. Nothing is committed to this repository, and the local volumes are disposable (`make reset` removes them). If you run it for any length of time, be aware that you are storing other people's content.

**What happens when someone retracts a post**, stated per store rather than in general, because it is not the same everywhere:

| Where the text lives | When a retraction removes it |
|---|---|
| `bsky_records_snapshot` (ClickHouse) | Hidden immediately, since `FINAL` stops returning a tombstoned key. Physically removed by `purge_deleted_records`, daily at 03:17. |
| `fct_posts` (dbt mart) | Within ~5 minutes. The mart is rebuilt from the `FINAL` view on that cadence, so each rebuild reconstructs it without the retracted rows and drops the old table. |
| Parquet landing archive | Not individually. Files are immutable by design, which is what makes replay trustworthy, so a retracted post's text remains in the file it arrived in until `purge_landing_archive` drops that whole file at the 7-day retention horizon. |

The archive is the weakest of the three, and the number is worth seeing. On a three-hour capture, 11,080 retracted posts still had their text in landing files against 1,466 in the warehouse. Retention bounds that exposure rather than eliminating it. Erasing a single record from an immutable archive would mean rewriting the file, which breaks the property replay depends on, so the honest guarantee is a horizon rather than an instant.

`dim_authors` and `agg_activity_by_minute` hold counts only, no record content.

**Contact:** ajohnson0764 [at] gmail [dot] com
