import json
import os
import signal
import sys
import time
from datetime import datetime, timezone

import requests
from confluent_kafka import Producer

# -------------------------
# Configuration
# -------------------------
BOOTSTRAP_SERVERS = os.environ.get("KAFKA_BOOTSTRAP_SERVERS", "kafka:9092")
TOPIC = os.environ.get("KAFKA_TOPIC", "financial_data")
POLL_INTERVAL = int(os.environ.get("POLL_INTERVAL_SECONDS", "30"))

API_URL = "https://api.coingecko.com/api/v3/simple/price"
COINS = os.environ.get("COINS", "bitcoin,ethereum,solana,cardano,polkadot")
CURRENCIES = os.environ.get("CURRENCIES", "usd,eur")

running = True

def signal_handler(sig, frame):
    global running
    print(f"Received signal {sig}, shutting down gracefully...")
    running = False

signal.signal(signal.SIGTERM, signal_handler)
signal.signal(signal.SIGINT, signal_handler)

# -------------------------
# Kafka producer setup
# -------------------------
def create_producer() -> Producer:
    config = {
        "bootstrap.servers": BOOTSTRAP_SERVERS,
        "acks": "all",
        "enable.idempotence": True,
        "retries": 5,
        "retry.backoff.ms": 500,
        "linger.ms": 100,
        "compression.type": "snappy",
    }
    return Producer(config)

def delivery_callback(err, msg):
    if err is not None:
        print(f"Message delivery failed: {err}")
    else:
        print(f"Delivered: {msg.topic()} [{msg.partition()}] @ {msg.offset()}")

# -------------------------
# Fetch and publish
# -------------------------
def fetch_prices() -> dict:
    params = {"ids": COINS, "vs_currencies": CURRENCIES}
    resp = requests.get(API_URL, params=params, timeout=10)
    resp.raise_for_status()
    return resp.json()

def publish_prices(producer: Producer, prices: dict):
    ts = datetime.now(timezone.utc).isoformat()
    for coin, price_data in prices.items():
        message = {"coin": coin, **price_data, "timestamp": ts}
        producer.produce(
            topic=TOPIC,
            key=coin,
            value=json.dumps(message),
            callback=delivery_callback,
        )
    producer.flush(timeout=10)

# -------------------------
# Main loop
# -------------------------
def main():
    print(f"Starting Kafka producer: {BOOTSTRAP_SERVERS} -> {TOPIC}")
    print(f"Tracking coins: {COINS}")
    print(f"Poll interval: {POLL_INTERVAL}s")

    producer = create_producer()
    backoff = 1

    while running:
        try:
            prices = fetch_prices()
            publish_prices(producer, prices)
            print(f"Published {len(prices)} price updates")
            backoff = 1
        except requests.RequestException as e:
            print(f"API request failed: {e}, retrying in {backoff}s")
            time.sleep(backoff)
            backoff = min(backoff * 2, 60)
            continue
        except Exception as e:
            print(f"Unexpected error: {e}")

        for _ in range(POLL_INTERVAL):
            if not running:
                break
            time.sleep(1)

    print("Producer shut down.")

if __name__ == "__main__":
    main()
