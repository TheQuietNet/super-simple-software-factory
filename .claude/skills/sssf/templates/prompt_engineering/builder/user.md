# Build Task

## Variables

### prompt

{{prompt}}

### previous_envelope

{{previous_envelope}}

### context_handoff_dir

{{context_handoff_dir}}

## Task

Implement the work described in `prompt`, guided by `previous_envelope` if present, then emit your `Report` JSON.

## Report

`changed_files` is every repo-relative path you wrote or edited this phase — the exact paths, including the test file. A JSON retry must still list them.

Your entire reply is one raw JSON object. No markdown fence. No prose before or after. Last format shown is the contract:

{
  "status": "success",
  "summary": "<one sentence describing what you built>",
  "changed_files": ["query.js", "tests/query-db.test.js"],
  "artifacts": [],
  "commit_message": "<imperative one-line git subject for the code you changed — this is what the commit of your work will say>",
  "notes_for_next_agent": "<how to verify this work>"
}
