# Changelog — Hubctl

## [0.1.0] — 31 juillet 2026

### Initial

- Extraction depuis POULIDOR (historique complet, 502 commits).
- Script wrapper bash `hubctl` + package Python `hubctl_cli/`.
- CLI TUI (menu, canvas, animations, widgets).
- Gestion des apps (start/stop/update/workshop), coffre (backup/restore), seed (export/deploy).
- Agent de debug (debug_agent) avec serveur MCP dédié (Docker/PSQL/file).
- 312 tests (pytest asyncio).
