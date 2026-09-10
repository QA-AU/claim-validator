# Contract Verifier Specification

## Purpose

Define the behaviour of the Endpoint Contract Verifier: given an OpenAPI
document and a running service, check each operation against its
documented contract and report the results.

## Requirements

### Requirement: OpenAPI document loading
The verifier SHALL accept an OpenAPI 3.x document supplied as either YAML
or JSON and parse it into an internal operation list.

#### Scenario: A YAML document is supplied
- **WHEN** the caller passes a file ending in `.yaml` or `.yml`
- **THEN** parse it as YAML
- **AND** produce one internal operation entry per path-and-method pair

#### Scenario: A JSON document is supplied
- **WHEN** the caller passes a file ending in `.json`
- **THEN** parse it as JSON
- **AND** produce the same internal operation list as the equivalent YAML

### Requirement: Request generation per operation
The verifier SHALL build exactly one request per operation, preferring the
`example` values declared in the document and falling back to values
generated from the parameter or schema type when no example is given.

#### Scenario: An operation declares examples
- **WHEN** an operation's parameters and request body all declare `example` values
- **THEN** use those example values verbatim in the generated request

#### Scenario: An operation declares no examples
- **WHEN** an operation parameter has a type but no `example`
- **THEN** generate a value of that type (string, integer, boolean, array, object)

### Requirement: Response validation
The verifier SHALL check that each response's status code is documented
for that operation and that the response body validates against the schema
the document associates with that status code.

#### Scenario: An undocumented status code is returned
- **WHEN** the service returns a status code not listed for the operation
- **THEN** mark the operation failed
- **AND** record the returned status and the list of documented statuses

#### Scenario: The response body is missing a required field
- **WHEN** the response body omits a field the schema marks as required
- **THEN** mark the operation failed
- **AND** record the missing field's name

### Requirement: Authentication passthrough
The verifier SHALL attach the caller-supplied bearer token to the
`Authorization` header of every request it sends, without modification.

#### Scenario: A bearer token is supplied
- **WHEN** the caller provides a bearer token
- **THEN** every generated request carries `Authorization: Bearer <token>`

### Requirement: Failure handling
The verifier SHALL record the operation, the expected value, and the
actual value for every failed check, and SHALL continue checking the
remaining operations.

#### Scenario: An operation fails midway through a run
- **WHEN** an operation's response fails a check
- **THEN** append its failure detail to the report
- **AND** proceed to the next operation without aborting the run

### Requirement: Rate limiting
The verifier SHALL send no more than 25 requests per second to the target
service.

#### Scenario: A large document is verified
- **WHEN** the document contains more operations than the per-second limit
- **THEN** spread the requests so no one-second window exceeds 25 requests

### Requirement: Retry on transient failure
The verifier SHALL retry a failed request up to three times with
exponential backoff before marking the operation failed.

#### Scenario: A request returns 503
- **WHEN** a request receives an HTTP 503 response
- **THEN** retry the request after a growing delay, up to three attempts
- **AND** mark the operation failed only if all attempts fail

### Requirement: Report format
The verifier SHALL emit a single JSON report containing the run totals
(operations checked, passed, failed) and a per-operation detail list.

#### Scenario: A run completes
- **WHEN** every selected operation has been checked
- **THEN** write a JSON object with `checked`, `passed`, and `failed` counts
- **AND** include a `operations` array with one entry per operation and its result

### Requirement: Write-operation protection
The verifier SHALL skip `POST`, `PUT`, `PATCH`, and `DELETE` operations
unless the caller explicitly enables write operations.

#### Scenario: Writes are not enabled
- **WHEN** the caller does not pass `--include-writes`
- **THEN** exercise only `GET` and `HEAD` operations
- **AND** list each skipped write operation in the report as `skipped`

### Requirement: Parallel execution
The verifier SHALL run up to 8 operations concurrently to reduce total run
time.

#### Scenario: A run has many independent operations
- **WHEN** more than 8 operations remain to be checked
- **THEN** keep 8 requests in flight at once until the queue drains

### Requirement: Created-resource cleanup
The verifier SHALL delete any resources it creates during a
write-enabled run before the run finishes.

#### Scenario: A write-enabled run creates a resource
- **WHEN** a `POST` operation creates a resource during the run
- **THEN** issue the corresponding `DELETE` for that resource before reporting
