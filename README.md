To use it immediately:



Hi All,

Thanks for looking at my repo. This is a mock orchestration platform that can be used in a production setting. I spent a while coding it so I'm pushing it to early (prospective) testers to see initial reactions, and opinions. The below is a preliminary README that I'll be changing it in the near future but it exists in the event that I don't have the time in the future to amend this. At a high level though, this repo contains a web server that serves the UI to clients, a daemon server that handles run launching, scheduling, and more, 2 grpc servers that are mocks of use cases, in this case an ETL job and ML job, an I/O manager for passing data between tasks, and a SQL database used for tracking metadata of events and also for the ETL and ML jobs to interact with. It also has logging, schema definitions, data contracts, and ideally a simple design. I did a multi-platform docker build that should allow anyone on Linux or Arm (Apple Silicon, for example) to be able to pull the images and run the code quickly, the install takes about a minute and a half or so. To start the code all you have to do is pull the code, go to the code directory and type make into the terminal. Feel free to reach out with any questions or comments!

Notes:

1) Should you test the platform, please be advised that the etl_job puts data into the database that the ml_pipeline_job pulls and trains on and thus the etl job should be run before the ml_pipeline_job. More importantly, etl_job runs the ddl to make all database tables as a consequence of me not wanting to construct the tables when the database started up since that would add complexity to the project. If you try to run the ml_pipeline_job first, it will fail due to not having the table in the database primarily, but it would also fail the asset check due to not having any data there either. The failing job, was designed to show what happens during different types of failures, for instance an asset failing a blocking asset check, and thus can be run whenever.

2) The current makefile in the project root (data_platfrom_sample) is one I made after I finished coding to allow prospective viewers a way to pull my docker images from dockerhub quickly and start the project without undergoing a long build process. Should people want to actually test the code, you would have to change the Makefile_dev.txt to a Makefile (you would likely have to rename the production Makefile to Makefile_prod.txt or so first), then you can run all the same make commands from the production Makefile in the dev one, as well as being able to run 'make build' which will build the code (this will take something like 20 minutes, I believe). The development makefile interacts with the docker compose file in the deployment directory and the prod makefile interacts with the docker compose file in the root directory.

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
