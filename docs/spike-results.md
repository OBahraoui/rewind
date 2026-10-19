# Duo Agent Platform spike — 2026-10-19 15:08 UTC

Project `87586132`, flow `developer/v1`, triggered: True.

| Probe | Result | Detail |
|---|---|---|
| Token and project | ✅ works | @othmane.bahraoui0904 on gitlab-ai-hackathon/transcend-october-2026/43319182/showcase (0.9s) |
| List agent privileges | ✅ works | 1=read_write_files, 2=read_only_gitlab, 3=read_write_gitlab, 4=run_commands, 5=use_git, 6=run_mcp_tools, 7=start_flows, 8=read_only_files (0.2s) |
| Create flow without starting it | ✅ works | id 9694184 (0.5s) |
| Callback hooks (callback_hook_id) | ✅ works | skipped: no project webhook to reference yet (run `rewind bootstrap` first) (0.2s) |
| Start flow developer/v1 | ✅ works | {'id': 9694186, 'web_url': 'https://gitlab.com/gitlab-ai-hackathon/transcend-october-2026/43319182/showcase/-/automate/agent-sessions/9694186', 'project_id': 87586132, 'namespace_id': None, 'agent_privileges': [1, 2, 3, 4, 5], 'agent_privileges_names': ['read_write_files', 'read_only_gitlab', 'read_write_gitlab', 'run_commands', 'use_git'], 'pre_approved_agent_privileges': [2], 'pre_approved_agent_privileges_names': ['read_only_gitlab'], 'workflow_definition': 'developer/v1', 'status': 'created', 'summary': None, 'title': 'developer/v1', 'allow_agent_to_request_user': True, 'image': None, 'environment': 'ambient', 'ai_catalog_item_version_id': 2517, 'workload': {'id': 3708131, 'message': None}, 'mcp_enabled': True, 'incremental_checkpoints_enabled': True, 'gitlab_url': 'https://gitlab.com'} (1.4s) |
| Status polling (GraphQL) | ✅ | 0s created → 53s running → 73s finished |
| Session trace (trace.jsonl) | ✅ works | 10 entries; tool calls: {'read_file': 1, 'run_command': 4} (0.5s) |
| Merge request created | ✅ works | !1 Draft: Spike: Duo flow (0.5s) |

Agent privileges available:

`1=read_write_files, 2=read_only_gitlab, 3=read_write_gitlab, 4=run_commands, 5=use_git, 6=run_mcp_tools, 7=start_flows, 8=read_only_files`

