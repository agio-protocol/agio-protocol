# AGIO Capacity Report

*Generated: 2026-04-21 09:19*

## Load Test Results

| Level | Success | Time | TPS | P50ms | P95ms | P99ms | Mem MB | Q Peak | Inv | Fail |
|---|---|---|---|---|---|---|---|---|---|---|
|         1K |    1,000 |    13.4s |       74 |   13.4 |   17.8 |   20.1 |     75 |    901 | ✅ |     0 |
|        10K |   10,000 |   141.9s |       70 |   14.0 |   19.0 |   23.4 |     68 |   9901 | ✅ |     0 |
|        50K |   49,511 |   706.8s |       70 |   13.6 |   20.0 |   24.2 |     52 |  49401 | ✅ |   489 |
|       100K |   55,778 |  1341.4s |       42 |   14.1 |   21.9 |   26.4 |     71 |  78588 | ✅ | 44222 |
|  sustained |    6,000 |   104.4s |       57 |   17.5 |   22.9 |   25.6 |     46 |      0 | ✅ |     0 |
| 100 agents |      992 |     3.3s |      300 |  282.0 |  772.0 |  912.9 |     83 |      0 | ✅ |     8 |
| 500 agents |    2,500 |     9.5s |      264 |  504.6 | 1015.5 | 6326.2 |     89 |      0 | ✅ |     0 |
|  1K agents |    3,000 |     9.1s |      331 |  438.4 |  653.8 | 7785.4 |     91 |      0 | ✅ |     0 |
|  2K agents |    4,000 |    14.5s |      277 |  511.7 | 1009.0 | 5920.6 |     93 |      0 | ✅ |     0 |

## Analysis

**Peak throughput:** 331 payments/second at 1K agents level
**Best P95 latency:** 17.8ms
**Peak memory:** 93 MB

### Breaking Point: 50K
- Failed payments: 489
- Error: 404: Agent not found: 0x000000000000000000000000000000000000000c
- Invariant: PASS

## Bottleneck Analysis

| Component | Observation |
|---|---|
| PostgreSQL | SELECT FOR UPDATE serializes concurrent payments per sender |
| Redis | Queue throughput ~5K-10K ops/sec per connection |
| Python | GIL limits true parallelism to ~1 core |
| Batch worker | Processes one batch at a time (serial) |

## Scaling Recommendations

| Daily Volume | Infrastructure Needed |
|---|---|
| 100K txns | 1 API + 1 worker + PG primary + Redis single |
| 500K txns | 3 API + 2 workers + PG primary+replica + Redis cluster |
| 1M txns | 5 API + 3 workers + PG cluster + Redis cluster + sharded queue |
