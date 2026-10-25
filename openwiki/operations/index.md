# Files

- [Local development and operations](local-development.md) - How to run billparse locally (Docker compose stack of PostgreSQL, API and web app, hot reload, or plain processes with SQLite), the Make targets, the environment variables, the service catalog refresh, and the known operational limits.
- [Storage, database and migrations](storage-and-database.md) - Where billparse keeps its state (database tables through SQLModel, large parts as files under the data directory), the Store API that wraps both, PostgreSQL versus SQLite, Alembic migrations, and how settings choose the locations.
