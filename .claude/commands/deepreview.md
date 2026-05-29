Run the built-in /review on the entire project first, then additionally analyze
the codebase for the following:

## Important instructions
- Do NOT assume package versions — read requirements.txt / pyproject.toml
- The codebase contains inline comments explaining design decisions. READ and
  RESPECT them before flagging issues. Quote the comment if it's relevant.
- After /review completes, continue with the extended analysis below.

## Extended scope (in addition to /review output)

### Security
- Injection vulnerabilities (SQL, command, path traversal)
- Hardcoded secrets, tokens, passwords
- Missing input validation / sanitization
- Insecure or unpinned dependencies (check requirements.txt)
- Exposure of sensitive data in logs or responses

### Python-specific
- Missing `await` on coroutines
- Sync blocking calls inside async handlers
- Mutable default arguments
- Bare `except:` clauses
- Circular imports, wildcard imports
- Unclosed resources (files, connections, sessions)
- Type hints completeness

### Performance
- N+1 query patterns
- Memory leaks (unclosed resources, growing collections)
- Unnecessary re-computation / missing caching

### Architecture
- Circular dependencies
- Violated separation of concerns
- Inconsistent patterns across similar modules

### Testing
- Missing tests for critical paths
- Untested error branches

## Output
Combine the /review findings with the above into a single Markdown report
saved to `review_report.md`. Use this structure:

# Code Review Report
**Date:** <today's date>
**Project:** <infer from project structure>

## Executive Summary

## Critical Issues 🔴

## Major Issues 🟠

## Minor Issues 🟡

## Positive observations ✅

## Recommendations

For each issue:
**[CATEGORY] Short title**
- File: `path/to/file.py:42`
- Description: what's wrong and why it matters
- Note: if an inline comment addresses this decision, quote it
- Suggestion: concrete fix