# Personal-agent API

The FastAPI service exposes a small machine-readable API for one private tutor
instance. It is not a public multi-tenant API.

## Authentication

Pass the generated token as a bearer credential:

```http
Authorization: Bearer YOUR_PRIVATE_TOKEN
```

Read endpoints also accept the secure dashboard session cookie. State-changing
endpoints require a bearer token even when a browser session is active, which
limits cross-site request risk. Unauthenticated loopback reads are disabled by
default and should remain disabled.

Never put the bearer token in a URL, repository, browser screenshot, or shell
history. The interactive OpenAPI schema is intentionally disabled; the raw
schema is available at `/api/v1/openapi.json`.

## Endpoints

| Method | Path | Result |
| --- | --- | --- |
| `GET` | `/api/v1/get_status` | Full runtime status projection. |
| `GET` | `/api/v1/get_readiness` | Curriculum ID and readiness domains. |
| `GET` | `/api/v1/get_active_assignment` | Public active assignment metadata or `null`. |
| `GET` | `/api/v1/get_review?assignment_id=A-0004` | Latest or selected completed review projection. |
| `POST` | `/api/v1/create_assignment` | Context-aware assignment creation through GitHub. |
| `POST` | `/api/v1/generate_report?period=weekly` | Weekly or monthly structured and Markdown report. |
| `POST` | `/api/v1/pause` | Pause new assignment creation. |
| `POST` | `/api/v1/resume` | Resume assignment creation. |

Health endpoints `/healthz` and `/readyz` are unauthenticated. The first proves
the process responds; the second also checks SQLite integrity and migrations.

## Read status

```bash
curl --fail \
  --header "Authorization: Bearer ${ADAPTIVE_TUTOR_API_TOKEN}" \
  http://127.0.0.1:8765/api/v1/get_status
```

The status contains paused state, active curriculum and assignment, readiness,
weaknesses, active misconceptions, upcoming reviews, recent scores and
activity, and model token/cost totals. Private tutor-host bundle material is
excluded.

## Read a completed review

```bash
curl --fail \
  --header "Authorization: Bearer ${ADAPTIVE_TUTOR_API_TOKEN}" \
  'http://127.0.0.1:8765/api/v1/get_review?assignment_id=A-0004'
```

Omit `assignment_id` for the latest review. The result contains the assignment,
selected qualitative review, dimension scores and rationale, detailed feedback,
follow-up decision, all attempts and their review scores, and the pull-request
URL when available. Private references, rubric, and evaluator guidance are
excluded. A missing review returns `404`.

## Create an assignment

```bash
curl --fail --request POST \
  --header "Authorization: Bearer ${ADAPTIVE_TUTOR_API_TOKEN}" \
  --header "Content-Type: application/json" \
  --data '{"available_minutes":30,"energy":"medium","days_until_goal":21}' \
  http://127.0.0.1:8765/api/v1/create_assignment
```

`available_minutes` is 5–480, energy is `low`, `medium`, or `high`, and the
optional goal horizon is 0–3650 days. `allowed_formats` can narrow selection to
known exercise-type strings. The endpoint returns `503` until GitHub access and
the protected evaluator control plane are both configured and verified.

The current construction build has no supported command to bootstrap the
workflow/key protections or required `evaluator_control_planes` record. Do not
hand-edit SQLite to make this endpoint proceed; remote assignment creation
remains unavailable until that authenticated setup path is implemented.

## Reports and control

```bash
curl --fail --request POST \
  --header "Authorization: Bearer ${ADAPTIVE_TUTOR_API_TOKEN}" \
  'http://127.0.0.1:8765/api/v1/generate_report?period=monthly'

curl --fail --request POST \
  --header "Authorization: Bearer ${ADAPTIVE_TUTOR_API_TOKEN}" \
  http://127.0.0.1:8765/api/v1/pause
```

Pausing does not stop evaluation, discard queued work, or terminate the
service. It gates adaptive assignment creation until `resume`.

## Security headers and limits

Every response disables caching and framing, rejects MIME sniffing, limits
referrers, and applies a script-free Content Security Policy. Login bodies are
bounded. Pydantic rejects unknown request fields and invalid values.

Bind to loopback and place any remote access behind authenticated TLS. A bearer
token is equivalent to personal-agent control over this learner instance.
