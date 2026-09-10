# Auth Tokens — delta spec

## ADDED Requirements

### Requirement: Token expiry
The service SHALL expire every issued access token after a fixed lifetime
and reject expired tokens on every protected endpoint.

#### Scenario: A token past its lifetime is used
- **WHEN** a request presents an access token issued more than 3600 seconds ago
- **THEN** respond with HTTP 401
- **AND** include an error code of `token_expired`

#### Scenario: A fresh token is used
- **WHEN** a request presents a token issued within the last 3600 seconds
- **THEN** process the request normally

## MODIFIED Requirements

### Requirement: Token issuance
The service SHALL issue an access token on a successful password grant,
and the response SHALL now also include a `refresh_token`.

#### Scenario: Successful password grant
- **WHEN** valid credentials are posted to `/oauth/token`
- **THEN** return an `access_token` and a `refresh_token`

## REMOVED Requirements

### Requirement: Static API keys
The service previously accepted a long-lived static API key in the
`X-API-Key` header. This is removed.
