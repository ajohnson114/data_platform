#!/usr/bin/env bash
set -e

USER=ajohnson0764
VERSION=1.0.0
PLATFORMS=linux/amd64,linux/arm64

# Ensure buildx builder exists and is active
docker buildx inspect multiarch-builder >/dev/null 2>&1 || \
  docker buildx create --name multiarch-builder --use

docker buildx inspect --bootstrap >/dev/null

# Webserver
docker buildx build \
  --platform $PLATFORMS \
  -f deployment/dockerfiles/webserver.dockerfile \
  -t $USER/data-platform-webserver:$VERSION \
  --push \
  .

# Daemon
docker buildx build \
  --platform $PLATFORMS \
  -f deployment/dockerfiles/daemon.dockerfile \
  -t $USER/data-platform-daemon:$VERSION \
  --push \
  .

# ML pipeline
docker buildx build \
  --platform $PLATFORMS \
  -f deployment/dockerfiles/ml_pipeline.dockerfile \
  -t $USER/data-platform-ml:$VERSION \
  --push \
  ./code_locations

# ETL pipeline
docker buildx build \
  --platform $PLATFORMS \
  -f deployment/dockerfiles/etl_pipeline.dockerfile \
  -t $USER/data-platform-etl:$VERSION \
  --push \
  ./code_locations

# DuckDB IO manager
docker buildx build \
  --platform $PLATFORMS \
  -f deployment/dockerfiles/duckdb_io_manager.dockerfile \
  -t $USER/data-platform-duckdb:$VERSION \
  --push \
  .
