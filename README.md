# Dagster Multi-Team Data Platform (Reference Implementation)

## Overview

This repository is a **reference implementation of a multi-team data orchestration platform built on Dagster**.

It shows how an organization can:

- Run **one centralized orchestration layer**
- Allow **multiple teams to deploy independently**
- Enforce **data validation before execution**
- Maintain **clear ownership boundaries**

Think of this project as:

> A platform design example for generalized orchestration tasks
> Not just a single pipeline

---

## Architecture Diagrams

**System design overview**

![System Design](docs/platform_sys_design.png)

**Asset execution and validation model**

![Asset Execution Model](docs/asset_execution_model.png)

---

## What This Demonstrates

This platform demonstrates how to:

- Isolate teams using separate code locations
- Enforce asset checks as hard execution gates
- Keep orchestration centralized but compute decentralized
- Prevent one team’s failure from breaking others
- Provide a clean path from local development to production

---

## Mental Model

### Control Plane (central Dagster daemon & webserver)

Responsible for:

- Scheduling
- Dependency resolution
- Run tracking
- Observability

### Execution Plane (team code locations)

Each team:

- Owns its assets
- Owns its checks
- Runs its own compute
- Deploys independently

The control plane never runs business logic.

---

## Key Guarantees

The platform enforces three core invariants:

### 1. Team Isolation

A failure in one team’s code cannot stop another team’s pipelines.

### 2. Validation-Gated Execution

Downstream assets **will not run** unless upstream checks pass.

Bad data cannot silently flow downstream.

### 3. Clear Ownership

Every asset and check has exactly one owning team.

No hidden coupling.

---

## Repository Structure (Simplified)

```text
code_locations/
  team_a/
  team_b/
  shared/
deployment/
workspace.yaml
```

- Each folder under `code_locations/` is a **team deployment unit**
- `shared/` contains reusable utilities
- Teams can be added without modifying existing teams

---

## Quick Start (Local)

### Prerequisites

- Docker Desktop  
- Git  
- macOS or Linux  

### Run the platform

```bash
git clone https://github.com/ajohnson114/data_platform.git
cd data_platform
make
```

Open the Dagster UI:

``` text
http://localhost:3000
```
## Example Execution Behavior

Two example jobs are included to demonstrate platform semantics.

### `etl_job`

- Creates database tables  
- Loads mock data  

### `ml_pipeline_job`

- Depends on ETL outputs  
- Intentionally fails if prerequisites are missing  

**This demonstrates:**

- Asset dependency enforcement  
- Validation gating  
- Failure visibility  

Some failures are intentional and part of the demo.

---

## Production Mapping (Conceptual)

This repo runs locally but maps cleanly to production:

| Local | Production |
|------|-----------|
| Docker Compose | Kubernetes / Helm |
| Local executor | Code location / K8sJobExecutor / Celery |
| DuckDB | S3 / data lake |
| Local Postgres | Managed cloud SQL |
| Makefile | CI/CD pipelines |

The architecture is designed so production hardening can be added **without changing core abstractions**.

---

## Why This Exists

This project is meant to demonstrate:

- Platform-level thinking  
- Correct abstraction boundaries  
- Multi-team scalability patterns  
- Validation-driven data systems  

---

## Usage Notice

This repository contains work samples for review purposes only.

- Commercial use is prohibited  
- Personal or educational use may be granted with permission  

**Contact:**  
ajohnson0764 [at] gmail [dot] com
