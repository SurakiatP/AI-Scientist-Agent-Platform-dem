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
