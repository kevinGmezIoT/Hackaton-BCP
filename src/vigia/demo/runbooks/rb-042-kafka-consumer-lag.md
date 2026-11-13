# RB-042: Kafka Consumer Group Lag

Conditions: Consumer lag growing continuously on ledger or event-streaming topics without database pool errors.

## Steps

1. Inspect consumer pod CPU and deserialization error logs for poison-pill payloads.
2. Route malformed messages to the dead-letter topic if schema validation fails.
3. Scale consumer replicas up to partition count if traffic burst caused lag.

