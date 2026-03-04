# Dagster Multi-Team Data Platform

A reference implementation of a production-style data orchestration platform built on Dagster. The goal is to demonstrate correct platform abstractions (but importantly not to prescribe specific infrastructure choices) so design decisions around executors, cost attribution, streaming, and scale are intentionally left open. The right answers depend on organizational context.

---

## Architecture

[![System Design](docs/platform_sys_design.png)](docs/platform_sys_design.png)

The platform separates two concerns:

**Control plane** (central Dagster daemon + webserver) handles scheduling, dependency resolution, run tracking, and observability. It never runs business logic.

**Execution plane** (team code locations) is where compute happens. Each team owns its assets, its checks, and its deployment lifecycle independently.

This boundary is the core invariant that makes multi-team scaling work. A bad deploy or runtime failure in one team's code location cannot affect another team's pipelines.

---

## Asset execution and validation

[![Asset Execution Model](docs/asset_execution_model.png)](docs/asset_execution_model.png)

Asset checks are first-class execution gates, not optional monitoring. Downstream assets will not run unless upstream checks pass — bad data cannot flow silently through the platform.

Checks are config-driven:

```yaml
asset_checks:
  cols_required_to_not_have_nulls: ["x_1", "x_2", "x_3", "x_4", "y"]
  expected_schema_from_etl_pipeline:
    x_1: { type: float, nullable: false }
    x_2: { type: float, nullable: false }
    x_3: { type: float, nullable: false }
    x_4: { type: float, nullable: false }
    y:   { type: float, nullable: false }
```

This drives schema drift detection automatically — if an upstream asset changes its output schema, the check fails before downstream work executes. Statistical drift detection can be layered on top by querying the warehouse for distribution statistics and asserting against a baseline inside an asset check.

---

## Repository structure

```
code_locations/
  team_a/       # Team A — owns its assets, checks, and compute
  team_b/       # Team B — owns its assets, checks, and compute
  shared/       # Shared utilities available to all teams
deployment/     # Deployment configuration
workspace.yaml  # Registers all code locations with Dagster
```

Adding a new team means adding a folder under `code_locations/` and registering it in `workspace.yaml`. No changes to existing teams required.

---

## Quick start (local)

**Prerequisites:** Docker Desktop, Git, macOS or Linux

```bash
git clone https://github.com/ajohnson114/data_platform.git
cd data_platform
make
```

Open the Dagster UI at `http://localhost:3000`.

Two example jobs are included:

- **`etl_job`** — creates tables, loads mock data, runs schema and null checks
- **`ml_pipeline_job`** — depends on ETL outputs, intentionally fails if upstream checks have not passed

Some failures are intentional. The point is to show validation gating and failure isolation in action, not a clean green run.

---

## Production mapping

| Local | Production |
|-------|------------|
| Docker Compose | Kubernetes / Helm |
| Local executor | K8sJobExecutor or CeleryExecutor |
| DuckDB | S3 / data lake / cloud warehouse |
| Local Postgres | Managed cloud SQL |
| Makefile | CI/CD pipelines |

A few production decisions worth noting explicitly:

**Secrets and config** are injected via Kubernetes Secrets and read-only ConfigMaps mounted into containers at known paths. Application code reads from those paths. No environment-specific logic lives in the codebase and no secrets are baked into image layers.

**Backfills** use Dagster's native partition system. Assets are defined with partition configs and backfills are triggered against specific partition ranges through the Dagster UI or API — idempotent and resumable without custom tooling.

**Executor choice** between K8sJobExecutor, CeleryExecutor, and others is a deployment decision left intentionally unspecified. The platform abstractions are the same regardless of which executor runs underneath.

**Streaming** is out of scope here. A platform serving real-time use cases would add a streaming layer (Kafka + Flink or Spark Streaming) alongside the batch layer. The Dagster orchestration layer would remain responsible for batch and scheduled work.

---

## Contact

ajohnson0764 [at] gmail [dot] com
