# GitHub App and webhooks

Remote learning uses one private learner workspace. Private curriculum packages
may live in a separate access-controlled repository, but the tutor loads them
from an operator-managed local checkout and its GitHub App does not need access
to that repository. Guided setup creates a dedicated App and limits its
installation to exactly the learning workspace; do not use an organization-wide
personal token for steady-state operation.

## Repository roles

| Repository | Contains | Access |
| --- | --- | --- |
| Learning workspace | Assignment branches, learner commits, pull requests, CI evidence, reviews | Learner + tutor App |
| Curriculum repository | Trusted references, private evaluator guidance, private package data | Operators only; local checkout configured in `curriculum_paths` |
| Public product | Generic engine, neutral bundled curriculum, docs, deployment templates | Public |

The public repository must never name or freeze private curriculum intent or
learner work.

## Guided App and workspace setup

Use `adaptive-tutor setup` through the trusted Compose `operator` service or
the equivalent systemd runbook. The setup process uses the operator's temporary
GitHub CLI login to:

1. create or verify the named private learning workspace;
2. open a browser GitHub App manifest under that same user or organization;
3. receive the App ID, private key, and webhook secret without printing them;
4. require installation on exactly the verified workspace;
5. install the evaluator and setup-probe workflows plus the public signing key;
6. protect and read back the default branch, then persist its immutable
   repository/workflow/key attestation; and
7. prove signed webhook delivery, a hosted Actions artifact, and the first PR.

The generated private App uses this callback:

```text
https://YOUR-TUTOR-HOST/webhooks/github
```

Its exact repository permissions are:

- metadata: read;
- contents: read/write for assignment branches;
- pull requests: read/write;
- Actions: read/write for trusted workflow dispatches;
- checks: read;
- issues: read/write for PR discussion commands; and
- no administration, secrets, environments, members, or repository-hook
  permission.

The App-level manifest webhook subscribes to `push`, `pull_request`,
`workflow_run`, `check_suite`, and `issue_comment`. Select only the newly
verified private workspace on GitHub's installation page, never "All
repositories". Setup rejects an installation token that can see any additional
repository.

GitHub generates one private key and webhook secret during manifest conversion.
The callback stores them in owner-only state and records only references in
YAML. After setup completes, remove the temporary `gh` login as shown in the
operations runbook; steady-state tutor and worker processes use only the App.
Rotate the App key and webhook secret after suspected exposure.

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

This manual shape is for inspection and recovery; guided setup writes it. Only
`https://api.github.com` is accepted. Raw secrets stay in the generated secrets
file or App key, not YAML.

```bash
adaptive-tutor doctor
adaptive-tutor doctor --live --strict
```

`doctor` authenticates once with the installation token to verify the private
workspace and exact one-repository scope. It separately signs an App JWT and
reads `GET /app` plus `GET /app/hook/config` to verify the configured App ID,
exact permissions/events, and App-level callback. The signed setup delivery is
the independent proof that the callback is active. `webhook-setup` is only for
legacy development-token mode with repository webhooks; do not run it after
guided App setup.

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

Guided setup protects the workspace default branch: at least one approving
review, stale-review dismissal, last-push approval, administrator enforcement,
linear history, conversation resolution, and no force pushes or deletion. It
then reads the protection back before attesting the evaluator controls. This is
default-branch protection, not an all-branch path ruleset. Organizations that
need defense in depth may add a separately audited ruleset restricting workflow
paths on learner branches, but must not claim guided setup created or verified
that rule.

Learner changes are also constrained by the signed assignment manifest's
allowed paths. The evaluator workflow is dispatched from the protected default
branch; assignment pushes never supply its executable definition.

The workflow uses a pinned action/toolchain, `persist-credentials: false`,
read-only job-token permissions, and an isolated credential-free environment
for learner code. Never use `pull_request_target`, or a workflow definition
loaded from an assignment branch, to execute learner content.

The repository includes the workflow contract at
`deploy/workspace/adaptive-tutor-evaluate.yml`; its protected workspace path is
`.github/workflows/adaptive-tutor-evaluate.yml`. The other protected control is
the tutor signing key's public half at
`.adaptive-tutor/evaluator-signing.pub`. The private half and complete assignment
bundles remain on the tutor host.

The `evaluator_controls` guided-setup step installs these files from the exact
public source revision, verifies the local/public evaluator-kit digest, applies
and reads back protection, verifies the workflow and key through the App, and
persists `evaluator_control_planes`. Do not hand-edit that record or weaken the
pre-publication verification.

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
