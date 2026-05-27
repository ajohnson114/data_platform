# -------------------------
# Environment selection
# -------------------------
ENV ?= dev
NL2SQL ?= 0

# -------------------------
# Paths
# -------------------------
PROJECT_ROOT := $(shell pwd)
DEPLOYMENT_DIR := $(PROJECT_ROOT)/deployment
COMPOSE_FILE := $(DEPLOYMENT_DIR)/docker-compose.yaml

# -------------------------
# Docker Compose
# -------------------------
COMPOSE = docker compose -f $(COMPOSE_FILE) $(if $(filter 1,$(NL2SQL)),--profile nl2sql,)

# -------------------------
# Targets
# -------------------------
.PHONY: dev uat prod dev-nl2sql up down build logs ps reset help

dev:
	@$(MAKE) up ENV=dev

dev-nl2sql:
	@$(MAKE) up ENV=dev NL2SQL=1

uat:
	@$(MAKE) up ENV=uat

prod:
	@$(MAKE) up ENV=prod

up:
	@echo "🚀 Starting environment: $(ENV)"
	cd $(DEPLOYMENT_DIR) && ENV=$(ENV) $(COMPOSE) up --build

down:
	@echo "🛑 Stopping environment: $(ENV)"
	cd $(DEPLOYMENT_DIR) && docker compose -f $(COMPOSE_FILE) --profile nl2sql down --remove-orphans

build:
	@echo "🔨 Building images for environment: $(ENV)"
	cd $(DEPLOYMENT_DIR) && ENV=$(ENV) $(COMPOSE) build

logs:
	cd $(DEPLOYMENT_DIR) && $(COMPOSE) logs -f

ps:
	cd $(DEPLOYMENT_DIR) && $(COMPOSE) ps

reset:
	@echo "⚠️  Resetting ALL containers and volumes (destructive)"
	cd $(DEPLOYMENT_DIR) && docker compose -f $(COMPOSE_FILE) --profile nl2sql down -v --remove-orphans

help:
	@echo ""
	@echo "Available commands:"
	@echo "  make dev         → start DEV environment"
	@echo "  make dev-nl2sql  → start DEV environment + NL-to-SQL interface (localhost:7860)"
	@echo "  make uat         → start UAT environment"
	@echo "  make prod        → start PROD environment"
	@echo "  make down        → stop environment"
	@echo "  make logs        → follow logs"
	@echo "  make reset       → remove containers and volumes"
	@echo ""
