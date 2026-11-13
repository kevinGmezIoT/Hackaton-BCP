# RB-087: Connection Pool Exhaustion Mitigation

Conditions: Connection pool exhausted errors concentrated in a newly deployed cohort or immediately following a database pool configuration change.

## Steps

1. Confirm whether `Connection pool exhausted` errors started after the active deployment and concentrate in the new release cohort.
2. Verify that concurrent request rate has not spiked independently of the configuration change.
3. Propose `rollback_change` on the causal release as the first mitigation action to restore the previous `pool_size`.
4. After human approval and rollback execution, verify that upstream p95 latency returns below the impact threshold.

