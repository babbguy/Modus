# Benchmarking Modus latency

Modus does not publish a fixed p99 number, because latency depends on your
hardware, database, and data volume. Instead it ships a reproducible harness so
you can measure Modus's **own overhead** — isolated from provider variance —
on your own deployment.

> The policy-evaluate path is not pre-cached: it performs a few small indexed
> database reads per call. Measure it yourself with the steps below and publish
> the number for *your* hardware.

## What to measure

- **Evaluate (warm path):** the enforcement decision latency for
  `POST /api/v1/evaluate/`. Bounded by `MODUS_ENFORCEMENT_TIMEOUT_MS` with a
  fail-open/closed fallback (`MODUS_ENFORCEMENT_FAIL_OPEN`), so it can never
  stall the caller past the timeout. (The SDK itself calls
  `POST /api/v1/policy/evaluate`, a separate endpoint that this script does not
  exercise.)
- **Gateway proxy overhead:** the proxy's added latency, measured against a
  fixed-latency mock upstream so the provider contributes zero noise. Overhead
  = measured total − mock delay.

## Method (isolate Modus's overhead)

1. Run on the target hardware. For a small-VPS figure, use a **1 vCPU / 1 GB**
   instance with the database local.
2. Seed realistic data — an empty database is not a meaningful benchmark.
   Create a team and an app (`POST /api/v1/apps/register` returns the `mds_`
   key used below; the app's slug is `--app-id`), policies, thresholds, and
   enough usage that the spend hierarchy is exercised. The `evaluate` mode
   authenticates with `X-Modus-APIKey`, which the `/api/v1/evaluate/` endpoint
   accepts only in `stub` auth mode, so benchmark a development instance
   (`MODUS_ENVIRONMENT=development`, `MODUS_AUTH_MODE=stub`).
3. For gateway overhead, start the mock upstream (fixed latency), point
   `MODUS_GATEWAY_OPENAI_BASE_URL` at it, and drive load.

```bash
# terminal 1 — mock OpenAI upstream with a fixed 40ms delay
python scripts/benchmark_gateway.py --serve-mock --mock-delay-ms 40 --mock-port 9099

# orchestrator config: point the gateway at the mock, enable it
export MODUS_GATEWAY_ENABLED=true
export MODUS_GATEWAY_OPENAI_BASE_URL=http://localhost:9099
python -m uvicorn orchestrator.main:app --port 8080   # in its own terminal

# terminal 2 — drive load
python scripts/benchmark_gateway.py evaluate \
    --base http://localhost:8080 --api-key mds_... --app-id bench-app \
    --n 500 --concurrency 20

python scripts/benchmark_gateway.py gateway \
    --base http://localhost:8080 --api-key mds_... \
    --n 500 --concurrency 20 --mock-delay-ms 40
```

The `gateway` mode prints both the total latency and the overhead (total minus
the mock delay). Report the overhead figure with the machine spec.

## Publishing a number

When you publish a p99, state:
- the hardware (vCPU / RAM / disk class),
- the database (SQLite vs PostgreSQL, local vs networked),
- the seeded data volume,
- concurrency and sample count.

A p99 without those qualifiers is not reproducible and should not be quoted.
