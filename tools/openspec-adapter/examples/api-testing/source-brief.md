# Endpoint Contract Verifier — product brief

## Problem

Teams publish an OpenAPI document and assume their running service matches
it. It drifts. We want a tool that takes the OpenAPI document and the base
URL of a live service and checks, operation by operation, that the service
actually behaves the way the document says.

## What it does

The Contract Verifier reads an **OpenAPI 3.x document** (YAML or JSON) and
the base URL of a running API. For **each operation** in the document it
sends one request, using the `example` values from the document where they
exist and generated values by type where they do not.

For every response it checks two things:

1. The **status code** returned is one the document lists for that
   operation. An undocumented status is a failure.
2. The **response body** validates against the schema the document gives
   for that status code. A missing required field, or a wrong type, is a
   failure.

## Authentication

The caller supplies a single **bearer token**. The verifier attaches it,
unchanged, to the `Authorization` header of every request it sends. It
does not manage login, refresh, or multiple tokens.

## Failure handling

When a check fails the verifier records the operation, the expected value,
and the actual value, and **continues the run**. One bad operation must
not stop the others from being checked.

## Reliability limits

- The verifier sends **no more than 10 requests per second** to the target
  service, so a large document does not look like an attack.
- On a **connection error or an HTTP 503**, it retries the request **once**.
  If the retry also fails, the operation is marked failed. No other status
  is retried.

## Safety

Runs are **read-only by default**: the verifier only exercises `GET` and
`HEAD` operations. `POST`, `PUT`, `PATCH`, and `DELETE` operations are
skipped unless the caller explicitly passes `--include-writes`.

## Output

A single **JSON report** containing the totals (operations checked,
passed, failed) and a per-operation detail list with the failure reason
for each failed operation.

## Explicitly out of scope for v1

Load or performance testing, security scanning, and stateful multi-step
scenarios. This tool checks one operation against its documented contract,
nothing more.
