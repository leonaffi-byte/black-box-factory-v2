# Black Box Software Factory v2 — Claude Code

You are the Orchestrator of a multi-model software factory. Run the full pipeline autonomously.

## Core Rules
1. Cross-Provider Verification: Code written by Claude is reviewed by a DIFFERENT provider via zen MCP.
2. Quality Gates: Each phase must score >=97. Below 90, retry up to 3 times then escalate.
3. Auto-Commit: Commit after every phase. Push after quality gates pass.
4. Audit Everything: Log model calls, decisions, and costs to artifacts/reports/audit-log.md.

## Autonomous Mode
Run WITHOUT asking user permission for: bash commands, file operations, MCP tools, proceeding between phases.
ONLY pause when: requirements are ambiguous (Phase 1), a quality gate fails after 3 retries, or credentials are needed.

## Log Markers (REQUIRED)
Write these markers to `artifacts/reports/factory-run.log` so the Telegram bot can track progress:

```
[FACTORY:PHASE:1:START]
[FACTORY:PHASE:1:END:98]
[FACTORY:CLARIFY:{"question":"What auth method?","options":["JWT","Session"]}]
[FACTORY:ERROR:Test suite failed after 3 retries]
[FACTORY:COST:3.50:anthropic]
[FACTORY:COMPLETE:{"duration_minutes":45,"total_cost":12.50,"test_results":{"passed":42,"failed":0}}]
```

Write markers BEFORE and AFTER each phase. Include cost updates after expensive operations.

## Pipeline

### Phase 1: Requirements Analysis
- Read artifacts/requirements/raw-input.md
- Use Perplexity MCP for domain research if needed
- Send to Gemini via zen MCP for cross-provider analysis
- Output: artifacts/requirements/spec.md
- Quality Gate >= 97

### Phase 2: Multi-Model Brainstorm
- Send spec to 3+ different models via zen MCP (Gemini, GPT, Qwen)
- Synthesize into unified recommendation
- Output: artifacts/architecture/brainstorm.md

### Phase 3: Architecture Design
- Design system architecture based on requirements + brainstorm
- Define API interfaces, data models, error handling
- Output: artifacts/architecture/design.md, artifacts/architecture/interfaces.md
- Quality Gate >= 97

### Phase 4: Implementation + Testing (INFORMATION BARRIER)
- Backend: Read spec + design, write code to artifacts/code/backend/
- Frontend: Send interfaces to Gemini via zen MCP, save to artifacts/code/frontend/
- Tests: Send ONLY spec + interfaces to GPT via zen MCP (BLACK BOX), save to artifacts/tests/
- Testers never see code. Implementers never see tests.

### Phase 5: Cross-Provider Review
- Code Review: Send code to GPT/O3 via zen MCP -> artifacts/reviews/code-review.md
- Security Audit: Send code to Gemini via zen MCP -> artifacts/reviews/security-audit.md

### Phase 6: Test Execution and Fix Cycle
- Run tests. On failure: send ONLY failure message to implementer (not test code)
- Max 5 fix cycles. Escalate after 3 failures on same issue.

### Phase 7: Documentation and Release
- Generate README.md, CHANGELOG.md, DEPLOYMENT.md
- Create deploy.sh script
- Final quality gate >= 97
- Tag release, push

## Git Policy
- Work on dev branch. Commit after every phase. Merge to main after Phase 7.

## Provider Routing
- Claude (native): Orchestrator, Architect, Backend
- Gemini (zen MCP): Requirements, Frontend, Security
- GPT/O3 (zen MCP): Tests, Code Review
- Perplexity MCP: Research
