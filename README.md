# Dagster-Based Data Orchestration Platform (Reference Implementation)

## Overview

This repository is a **reference implementation of a multi-team data orchestration platform built on top of Dagster**.

It demonstrates how to use Dagster as an **execution substrate** while layering **platform-level abstractions** that enforce:

- Platform-level thinking
- Team isolation
- Standardized pipeline structure
- Validation-driven execution
- Clear separation between platform logic and business logic

This is a **platform**, not a single pipeline.

The project is intentionally scoped to focus on **architecture, contracts, and execution semantics**, rather than cloud deployment, vendor-specific infrastructure, or paid external services.

---

## What This Project Is (and Is Not)

### This project *is*:
- A Dagster-based **data orchestration platform abstraction**
- A demonstration of **organizational scalability and team isolation patterns**
- A local-first, reproducible execution environment
- A systems design artifact intended for review and discussion

### This project is *not*:
- A production deployment
- A replacement for Dagster, Airflow, or Temporal
- An example of cloud provisioning or Kubernetes operations
- An endorsement of training ML models inside Dagster

Infrastructure concerns (Kubernetes, cloud storage, secrets management, CI/CD) are intentionally **out of scope**, but conceptual production mappings are documented.

---

## Architecture & Systems Design

Detailed architectural discussion, design rationale, and tradeoffs are documented in:

**ARCHITECTURE.md**  
https://github.com/ajohnson114/data_platform/blob/main/docs/ARCHITECTURE.md

Topics covered include:
- Platform vs pipeline responsibilities
- Dagster’s role in the stack
- Validation and execution guarantees
- Team isolation boundaries
- Conceptual production deployment mappings

---

## Local Execution (Quick Start)

This project is designed to be run **locally** with no cloud dependencies.

### Prerequisites
- Docker Desktop (installed and running)
- Git
- Linux or macOS
- Multi-architecture Docker images supporting both ARM and AMD systems

### Steps

```bash
git clone https://github.com/ajohnson114/data_platform.git
cd data_platform
make
```

Once the platform is running, open the Dagster UI at:

http://localhost:3000

# Execution Notes & Intended Behavior

## Pipeline Ordering & Failure Semantics
etl_job must be run before ml_pipeline_job

### etl_job
Creates database tables (DDL)
Loads mock data into the database
### ml_pipeline_job
Depends on outputs produced by etl_job
Will intentionally fail if prerequisites are missing

## This behavior is intentional and demonstrates:
Asset dependency enforcement
Blocking asset checks
Failure propagation and visibility within Dagster
Some failing jobs exist by design to illustrate platform behavior under different failure modes.

## Makefile & Development Workflow
Two Makefiles are provided to support different review and development workflows.
### Production-Style Makefile (Default)
Pulls prebuilt Docker images from Docker Hub
Optimized for fast startup and reviewer convenience
Used by the default make command
### Development Makefile
Builds Docker images locally
Build time is approximately 20 minutes
Intended for reviewers who want to inspect, modify, or extend the code

### To use the development workflow:
Rename Makefile_dev.txt to Makefile
Rename the existing Makefile to Makefile_prod.txt

Run:

make build

The development Makefile uses the Docker Compose configuration in the deployment/ directory, while the production Makefile uses the root-level Docker Compose configuration.

# Machine Learning Pipeline (Scope Clarification)
A simple machine learning pipeline is included only as a downstream consumer of the data platform.
This is not an endorsement of training ML models in Dagster.
In production environments, ML training and serving typically require:
Heterogeneous cluster scaling
Specialized schedulers
Dedicated MLOps infrastructure

## The ML pipeline exists solely to demonstrate:
Cross-team dependency orchestration
Platform support for heterogeneous workloads
Production Mapping (Conceptual)

# Although this repository runs locally, it is designed to map cleanly to production Dagster deployments:
Local Component	Production Equivalent
Local Dagster	Dagster on Kubernetes / Dagster Cloud
Docker Compose	Helm / Terraform
Local executor	KubernetesJobExecutor / CeleryExecutor
Mock data sources	S3 / GCS / External APIs
Makefile	CI/CD pipelines
These mappings are discussed in more detail in ARCHITECTURE.md.

# Why This Exists
This project exists to demonstrate:
Platform-level thinking
Correct abstraction boundaries
Validation-driven execution
Team isolation and organizational scalability
Judgment about what not to build
It is intended to be read, reviewed, and discussed as a systems design and platform engineering artifact, not simply executed as a demo.

# Usage Notice
This repository contains work samples for review purposes only.
Commercial use is prohibited
Personal or educational use may be granted with permission
Please contact:
ajohnson0764 [at] gmail [dot] com