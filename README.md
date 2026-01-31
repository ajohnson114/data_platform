# To use the platform immediately:

Preliminary Step: To use this, you need to have docker desktop installed and open. A multi-platform build was done, so arm and amd users should be able to access this demo. 

Step 1) In your terminal enter and execute the below command:

git clone https://github.com/ajohnson114/data_platform.git

Step 2) Move to the directory on your local computer with the following command (Linux based):

cd ./data_platform

Step 3) Enter the following command and let the application start up:

make

Step 4) Open a new tab and go to the following port on your computer (Copy and paste it into your search bar and hit Enter):

localhost:3000

# Systems design

To see more information about the architecture and some systems design discussion, please see the ARCHITECTURE.md in the docs directory.

Quick link: https://github.com/ajohnson114/data_platform/blob/main/docs/ARCHITECTURE.md

# Notes:

1) Should you test the platform, please be advised that the etl_job puts data into the database that the ml_pipeline_job pulls and trains on and thus the etl job should be run before the ml_pipeline_job. More importantly, etl_job runs the ddl to make all database tables as a consequence of me not wanting to construct the tables when the database started up since that would add complexity to the project. If you try to run the ml_pipeline_job first, it will fail due to not having the table in the database primarily, but it would also fail the asset check due to not having any data there either. The failing job, was designed to show what happens during different types of failures, for instance an asset failing a blocking asset check, and thus can be run whenever.

2) The current makefile in the project root (data_platfrom_sample) is one I made after I finished coding to allow prospective viewers a way to pull my docker images from dockerhub quickly and start the project without undergoing a long build process. Should people want to actually test the code, you would have to change the Makefile_dev.txt to a Makefile (you would likely have to rename the production Makefile to Makefile_prod.txt or so first), then you can run all the same make commands from the production Makefile in the dev one, as well as being able to run 'make build' which will build the code (this will take something like 20 minutes, I believe). The development makefile interacts with the docker compose file in the deployment directory and the prod makefile interacts with the docker compose file in the root directory.

3) My inclusion of a machine learning pipeline in this example isn't an endorsement of training ML models in dagster as opposed to it being a workflow that I felt made sense and was simple with the etl pipeline. I'd recommend a more robust machine learning set up (I did an end to end MLOps project a few years ago that is on my GitHub) for training and serving since they often need things like heterogenous cluster scaling and the like which dagster isn't good at.

# Data Platform Sample

## Overview

This repository is a **reference implementation of a multi-team data orchestration platform** designed to support independent data pipelines owned by different teams while providing consistent execution semantics, validation guarantees, and operational clarity.

The goal of this project is to demonstrate how a centralized orchestration layer can support **many independent teams** without requiring shared business logic, shared compute, or tight coupling between pipelines.

This is a *platform*, not a pipeline.

---

## Why This Exists

This project exists to demonstrate:
- Platform-level thinking
- Team isolation patterns
- Validation-driven data execution
- Clear separation of concerns between platform and business logic

It is meant to be read as a **systems design artifact**, not just executed.

## Usage Notice

This repository contains work samples for review purposes only.  
Use requires explicit permission from the author.  
Personal or educational use may be granted; commercial use is prohibited.  
Please contact: ajohnson0764[at]gmail[dot]com
