# ModMesh development

- Implement one meaningful roadmap step at a time. Build and exercise it before committing.
- Keep game adapters, source adapters, and the core independent. No game-specific paths or source API payloads in the core or UI.
- Never identify mods using their names alone. File equality requires verified content hashes.
- Plan filesystem changes before applying them; preserve unmanaged user files and recover from failures.
- Do not add LICENSE files or license claims unless explicitly requested.
- Do not create test suites, fixtures, sample projects, or dummy data by default. Verify with real entry points and temporary, inline checks outside the repository; add permanent tests only when necessary to solve an actual problem.
- Do not add game launching, game servers, LAN/VPN, chat, save synchronization, or forced updates.
- Keep secrets, caches, databases, build output, and machine-specific paths out of git.
- Update docs/roadmap.md and the README to reflect working functionality, not planned features.

Stack: Python 3.12, PySide6/Qt Widgets, httpx, SQLite/SQLAlchemy, FastAPI/PostgreSQL. Use docs/development-plan.md as the authoritative plan. ARK means Survival Evolved Steam, never Ascended. Core must never import Qt. Verify Actions after each push; fix failures before advancing.
