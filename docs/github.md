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
- Actions: read/write for trusted workflow dispatches;
- checks: read;
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

Protect the workspace default branch. Disallow force pushes and deletion,
restrict changes to evaluator/workflow paths, and require the tutor App or an
operator review for those protected files. Learner
branches should change only assignment-visible paths. Apply a repository
ruleset that blocks learner writes to `.github/workflows/**` on every branch;
default-branch protection alone does not prevent a new branch workflow from
requesting a self-hosted runner.

The evaluator workflow is dispatched by the tutor on the protected default
branch; assignment pushes never supply its executable definition. It uses a
pinned action/toolchain, `persist-credentials: false`, read-only job token
permissions, and an isolated credential-free environment for learner code.
Never use `pull_request_target`, or a `push` workflow loaded from an assignment
branch, to execute learner content.

The repository includes a hardened workspace template at
`deploy/workspace/adaptive-tutor-evaluate.yml`. Install it as
`.github/workflows/adaptive-tutor-evaluate.yml` on the protected default branch.
It runs only on dedicated, one-job runners carrying the
`adaptive-tutor-ephemeral` label. The runner must be destroyed after the job; it
must never be the tutor host or a machine holding GitHub-write, model, personal
agent, or dashboard credentials.

The tutor signs and writes an assignment-and-branch-bound envelope to its
owner-only evaluator spool before it calls GitHub to create the branch or pull
request. The signed push webhook then records the submission and dispatches the
protected workflow with a typed assignment ID, branch, and commit. Its run title
exposes those bounded identifiers to the runner autoscaler.

Before registering the ephemeral runner, the trusted provisioner runs
`adaptive-tutor stage-evaluator` with the queued run ID and exact identity. The
command verifies the workflow provenance before placing the
short-lived envelope at `$RUNNER_TEMP/trusted/assignment-bundle.json` and its
public key at `$RUNNER_TEMP/trusted/evaluator-signing.pub`, both mode `0600`.
The private signing key remains on the tutor host. These files arrive out of
band from protected tutor state, never from the learner branch or an Actions
artifact.

The workflow checks both files and modes, checks out the exact input commit
without retained credentials, invokes the hidden `adaptive-tutor evaluate`
command with an empty environment, writes evidence outside the checkout, and
uploads exactly `adaptive-tutor-evidence.json`. The evaluator authenticates the
signature and requires the envelope assignment, branch, commit, expiry, and
digest to match the public assignment manifest before it consumes the staged
files and starts learner tests in a scrubbed, resource-limited temporary
directory. A
missing, substituted, replayed, symlinked, or broadly readable envelope fails
closed.

The tutor accepts a run only when its workflow ID and path, repository, head
repository, `workflow_dispatch` event, typed run identity, default branch, and
unchanged default-branch workflow digest all match. The normalized artifact's
internal assignment ID, commit SHA, schema, and digest must then match before
qualitative review starts.

## Reconciliation and recovery

Webhooks are primary; polling is only a repair mechanism. After an outage:

- inspect delivery failures and redeliver from GitHub;
- run `doctor` to verify repository and webhook state;
- allow durable queued jobs and expired leases to resume;
- reconcile the active pull request and workflow run; and
- confirm duplicate deliveries remain no-ops.

See [Operations](operations.md#failure-recovery) for host recovery and
[Security](security.md) for the untrusted execution boundary.
