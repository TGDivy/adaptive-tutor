# GitHub App and webhooks

Remote learning uses two private repositories: a learner workspace and a
separate curriculum repository. Install a dedicated GitHub App only
on those selected repositories; do not use an organization-wide personal token.

## Repository roles

| Repository | Contains | Access |
| --- | --- | --- |
| Learning workspace | Assignment branches, learner commits, pull requests, CI evidence, reviews | Learner + tutor App |
| Curriculum repository | Trusted references, private evaluator guidance, private package data | Tutor App/operators only |
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

These commands configure repository access and webhook delivery only. They do
not install or record the protected evaluator control plane described below.

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
operator review for those protected files. Learner branches should change only
assignment-visible paths. Apply a repository ruleset that blocks learner writes
to `.github/workflows/**` on every branch; default-branch protection alone does
not prevent an assignment branch from introducing an executable workflow.

The evaluator workflow is dispatched by the tutor on the protected default
branch; assignment pushes never supply its executable definition. It uses a
pinned action/toolchain, `persist-credentials: false`, read-only job-token
permissions, and an isolated credential-free environment for learner code.
Never use `pull_request_target`, or a workflow definition loaded from an
assignment branch, to execute learner content.

The repository includes the workflow contract at
`deploy/workspace/adaptive-tutor-evaluate.yml`; its protected workspace path is
`.github/workflows/adaptive-tutor-evaluate.yml`. The other protected control is
the tutor signing key's public half at
`.adaptive-tutor/evaluator-signing.pub`. The private half and complete assignment
bundles remain on the tutor host.

> **Construction status:** the current public CLI does not install these files,
> establish their branch protections, or populate the required
> `evaluator_control_planes` state. Do not hand-edit SQLite to bypass that
> check. Remote assignment publication is not supported end to end until an
> authenticated bootstrap and trust-anchor rotation path lands.

## Signed public evaluation

For each assignment, the tutor keeps the complete bundle, including private
references, rubric, and evaluator guidance, in owner-only host state. It
publishes only safe assignment files plus
`.adaptive-tutor/evaluator-manifest.json`. That Ed25519-signed public manifest
binds the assignment and branch, allowed submission files, learner-visible
public-test digests, fixed evaluator command and limits, evaluator-kit digest,
and key ID. Public tests are visible by design; changing their bytes invalidates
the signed contract.

On a learner push, the tutor first verifies the protected workflow digest,
public-key ID, immutable repository ID, and default-branch state against its
provisioned control record. It stores a unique dispatch nonce and dispatches the
workflow with the exact learner commit, manifest digest, public evaluator source
commit, and evaluator-kit digest.

The workflow runs on GitHub-hosted `ubuntu-24.04`. It checks out the protected
workflow and verification key at `github.workflow_sha`, the public evaluator at
the exact `evaluator_ref`, and the learner commit into three separate
directories. It recomputes the evaluator-kit digest, installs the locked
runtime and Bubblewrap, and starts `adaptive_tutor.public_evaluator` under
`env -i`. Learner code runs in a read-only, networkless Bubblewrap namespace
with bounded resources. No private bundle or tutor/model credential enters the
job, and raw learner output remains quarantined.

The job uploads only `adaptive-tutor-evidence.json`. The tutor accepts a run
only when its workflow ID/path, repository and head repository,
`workflow_dispatch` event, default branch, typed run title, workflow commit and
digest, and repository ID match stored protected state. The artifact must also
match the stored assignment, learner commit, dispatch nonce, manifest digest,
workflow/evaluator commits and digests, evaluator key ID, and repository ID
before qualitative review starts.

## Reconciliation and recovery

Webhooks are primary; polling is only a repair mechanism. After an outage:

- inspect delivery failures and redeliver from GitHub;
- run `doctor` to verify repository and webhook state;
- allow durable queued jobs and expired leases to resume;
- reconcile the active pull request and workflow run; and
- confirm duplicate deliveries remain no-ops.

See [Operations](operations.md#failure-recovery) for host recovery and
[Security](security.md) for the untrusted execution boundary.
