# Project rules

- FastAPI is the authoritative game server.
- PostgreSQL/Neon stores persistent game data.
- Clients never calculate authoritative combat results.
- Abilities, weapons, enemies, and progression data should be database-driven where reasonable.
- Game rules and execution logic stay on the server.
- Avoid hard-coding content when it belongs in the database.
- Prefer reusable systems over special-case logic.
- Reuse existing abstractions. Find and extend the current implementation before introducing a new one.
- Do not create a second damage calculation path; extend the current damage resolver.
- Do not add a new endpoint when an existing endpoint, such as the encounter action endpoint, can support the behavior cleanly.
- Do not redesign unrelated systems unless explicitly asked.
- Preserve existing API behavior unless the task requires changing it.
- Use migrations for schema changes.
- Add tests for new combat behavior.
- Read [docs/architecture.md](docs/architecture.md) before making architectural changes.
- Keep the architecture document short and update it only when the architecture itself changes.

# Change workflow

Before making changes, summarize:

1. What files you think need to change.
2. What database changes are required.
3. What existing behavior could be affected.

Then make the changes.
