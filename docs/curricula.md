# Curriculum authoring

Curricula are data packages. Core scheduling, persistence, evaluation, and
GitHub code do not import a subject module or hard-code a domain.

## Package layout

```text
my-curriculum/
├── curriculum.yaml
├── concepts.yaml
├── prerequisites.yaml
├── profiles.yaml
├── prompts/
│   ├── generation.md
│   └── grading.md
├── references/
│   └── trusted-source.md
└── fixtures/
    └── example-evaluation.json
```

The four YAML files and both prompts are required. Fixtures are optional but
recommended for deterministic tests. Reference paths must resolve beneath the
package's `references/` directory.

## Metadata

```yaml
id: example-foundations
name: Example Foundations
version: 1.0.0
description: A practical, evidence-oriented example curriculum.
default_profile: generalist
generation_guidance: Prefer bounded tasks with observable outcomes.
grading_guidance: Separate correctness, reasoning, trade-offs, and style.
```

IDs are lowercase slugs, versions are semantic `major.minor.patch`, and all
free-text guidance must be substantive. Changing package content changes its
stored SHA-256 digest; changing grading behavior should also advance the
version.

## Concepts

```yaml
concepts:
  - id: programming.state-invariants
    name: State invariants
    domain: programming
    description: State and preserve invariants across transitions.
    importance: 1.4
    base_difficulty: 4
    exercise_types: [debugging, code_review, written]
    reference_files: [trusted-source.md]
    generation_guidance: Include at least one boundary transition.
    grading_guidance: Require the invariant to be stated explicitly.
```

Importance is between 0.1 and 2.0, base difficulty is 1–10, and each concept
supports at least two known exercise types. Concept IDs must be unique across
the loaded database, so prefix them with a stable curriculum/domain namespace.

## Prerequisites

```yaml
prerequisites:
  programming.state-invariants:
    - programming.basic-control-flow
```

Every referenced concept must exist, self-dependencies are rejected, and the
entire graph must be acyclic. Prerequisites influence scheduling rather than
acting as irreversible locks.

## Profiles

```yaml
profiles:
  - id: generalist
    name: Generalist
    description: Balanced emphasis across the package.
    domain_weights:
      programming: 1.0
    concept_weights:
      programming.state-invariants: 1.2
```

Weights are positive and no greater than 3. A profile can combine broad domain
emphasis with precise concept overrides. The metadata default must name an
existing profile.

## Trusted prompts and references

Generation and grading prompts define curriculum intent but remain separate
from learner submission text. References are private trusted expectations, not
files to publish on the assignment branch. Keep licenses and provenance clear,
and use concise reference material that supports evaluation rather than leaking
a complete answer.

Private curricula belong in a separate access-controlled repository. Add its
local checkout to `curriculum_paths`; never copy it into the public product
repository, screenshots, fixtures, or support bundles.

## Validate and load

```bash
adaptive-tutor curriculum-load /secure/path/my-curriculum
adaptive-tutor doctor --offline
adaptive-tutor next --dry-run --json
```

Loading validates the complete package before a transaction persists metadata,
concepts, relationships, profiles, prompt versions, digest, and initial mastery
rows. Test packages should also exercise graph failures, invalid references,
fixture schemas, and assignment generation for every supported format.
