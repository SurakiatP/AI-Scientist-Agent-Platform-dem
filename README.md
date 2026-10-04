# AI-Scientist-Agent-Platform-dem

## Local configuration

Copy `.env.example` to `.env` and configure PostgreSQL, local MinIO and the
internal secret settings. `.env` is ignored by Git; `.env.example` contains only
placeholders. Keep the encryption key file under the ignored `.local/` directory.

Python commands load this file explicitly with `uv run --env-file .env <command>`.
The application does not automatically load `.env`. Public API startup and
deployment setup are still being implemented.

Provider API keys follow the Settings and encrypted-credential flow. Direct
provider environment variables such as `OPENAI_API_KEY` are not wired into the
application yet.

## Implementation status

This repository contains an implementation in progress. The database records,
owner authorization, encrypted credentials, run lifecycle, effect journal,
object/checkpoint boundaries and bilingual project/library interface are present.
The isolated runtime adapter is being validated; a source merge does not mean
the platform is ready for deployment.

Remaining work includes the full runtime recovery and containment acceptance
matrix, the paper search and citation workflow, chat and artifact interfaces,
live API/event integration, MCP and bidirectional A2A adapters, and local
deployment, backup/restore and final security acceptance. See the staged
[implementation plan](docs/superpowers/plans/2026-10-03-platform-build.md)
for dependencies and completion gates.
