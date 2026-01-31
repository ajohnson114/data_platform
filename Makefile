# -------------------------
# Environment selection
# -------------------------
ENV ?= dev

# -------------------------
# Paths
# -------------------------
PROJECT_ROOT := $(shell pwd)
COMPOSE_FILE := $(PROJECT_ROOT)/docker-compose.yaml

# -------------------------
# Docker Compose
# -------------------------
COMPOSE = docker compose -f $(COMPOSE_FILE)

# -------------------------
# Targets
# -------------------------
.PHONY: dev uat prod up down logs ps reset help

dev:
	@$(MAKE) up ENV=dev

uat:
	@$(MAKE) up ENV=uat

prod:
	@$(MAKE) up ENV=prod

up:
	@echo "🚀 Starting environment: $(ENV)"
	ENV=$(ENV) $(COMPOSE) up 

down:
	@echo "🛑 Stopping project!"
	$(COMPOSE) down

logs:
	$(COMPOSE) logs -f

ps:
	$(COMPOSE) ps

reset:
	@echo "⚠️  Resetting ALL containers and volumes (destructive)"
	$(COMPOSE) down -v

help:
	@echo ""
	@echo "Available commands:"
	@echo "  make dev     → start DEV environment"
	@echo "  make uat     → start UAT environment"
	@echo "  make prod    → start PROD environment"
	@echo "  make down    → stop environment"
	@echo "  make logs    → follow logs"
	@echo "  make reset   → remove containers and volumes"
	@echo ""
