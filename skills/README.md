# Test Atelier skills

The application keeps a project-level registry of the QA skills it can run.
The skill content is loaded from the configured `qa-doc-skills` workspace at
runtime; this directory stores the registry contract used by the API and UI.

Set `TEST_ATELIER_SKILL_ROOT` when the skills workspace is not at the default
development path:

```bash
export TEST_ATELIER_SKILL_ROOT=/Users/lazy/huangsheng/project/qa-doc-skills
```

The registry intentionally follows the source repository's Hub routing rules:
one request selects one child skill, and requirement analysis is completed
before test-case generation is started.
