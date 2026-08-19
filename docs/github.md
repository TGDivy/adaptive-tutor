# GitHub App and webhooks

Remote learning uses two private repositories: a learner workspace and a
separate curriculum/evaluator repository. Install a dedicated GitHub App only
on those selected repositories; do not use an organization-wide personal token.

## Repository roles

| Repository | Contains | Access |
| --- | --- | --- |
| Learning workspace | Assignment branches, learner commits, pull requests, CI evidence, reviews | Learner + tutor App |
| Curriculum repository | Trusted references, hidden evaluator guidance, private package data | Tutor App/operators only |
| Public product | Generic engine, neutral bundled curriculum, docs, deployment templates | Public |

The public repository must never name or freeze private curriculum intent or
learner work.

## Create the App

Create a GitHub App under the account that owns the two private repositories.
Use a descriptive private name and set the webhook callback to:

```text
https://YOUR-TUTOR-HOST/webhooks/github
```

Grant only the repository capabilities needed by your deployment:

- metadata: read;
- contents: read/write for assignment branches;
- pull requests: read/write;
- Actions and checks: read;
- issues: read/write for PR discussion commands; and
- repository webhooks: read/write only if using `webhook-setup` reconciliation.

Subscribe to `push`, `pull_request`, `workflow_run`, `check_suite`, and
`issue_comment`. Install the App on selected private repositories, not every
current and future repository.

Generate one private key, store it in an owner-only file outside source
control, and record the App and installation IDs. Rotate the key and webhook
secret on a defined schedule and immediately after suspected exposure.

## Configure Adaptive Tutor

The relevant YAML shape is:

```yaml
github:
  owner: your-github-owner
  workspace_repo: learning-workspace
  curriculum_repo: private-curricula
  api_url: https://api.github.com
  app_id: 12345
  installation_id: 67890
  private_key_path: /secure/path/github-app.pem
  webhook_url: https://tutor.example.net
  token_env: ADAPTIVE_TUTOR_GITHUB_TOKEN
  webhook_secret_env: ADAPTIVE_TUTOR_WEBHOOK_SECRET
```

Only `https://api.github.com` is accepted by the public build. Raw secrets stay
in the generated secrets file, an owner-only worker environment file, or the
App key—not YAML.

```bash
adaptive-tutor doctor
adaptive-tutor webhook-setup
```

`doctor` verifies that the workspace is private and writable, the configured
callback is active with required events, and the App key and webhook secret are
present. `webhook-setup` is idempotent: it reconciles an existing matching hook
or creates one.

## Webhook request path

For every delivery, the service:

1. reads a bounded request body;
2. verifies `X-Hub-Signature-256` with constant-time HMAC comparison;
3. validates event and delivery identifiers;
4. rejects events outside the exact configured private workspace;
5. inserts the delivery ID exactly once; and
6. enqueues a durable job before responding `202`.

Long GitHub, artifact, and model work never happens in the HTTP request. A
replayed delivery returns the original event/job identity with `duplicate:
true` and does not create duplicate learner evidence.

## Branch and workflow protection

Protect the workspace default branch. Require the evaluator check, disallow
force pushes and deletion, restrict changes to evaluator/workflow paths, and
require the tutor App or an operator review for those protected files. Learner
branches should change only assignment-visible paths.

The evaluator workflow must use a trusted default-branch definition, a pinned
action/toolchain, `persist-credentials: false`, read-only token permissions,
and an isolated credential-free container for learner code. Never use
`pull_request_target` to execute a pull request checkout with write credentials.

## Reconciliation and recovery

Webhooks are primary; polling is only a repair mechanism. After an outage:

- inspect delivery failures and redeliver from GitHub;
- run `doctor` to verify repository and webhook state;
- allow durable queued jobs and expired leases to resume;
- reconcile the active pull request and workflow run; and
- confirm duplicate deliveries remain no-ops.

See [Operations](operations.md#failure-recovery) for host recovery and
[Security](security.md) for the untrusted execution boundary.
