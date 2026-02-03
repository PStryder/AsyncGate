# Local Development with Docker

This guide covers running AsyncGate locally using Docker Compose for development and testing.

## Prerequisites

- Docker Desktop installed and running
- Docker Compose (included with Docker Desktop)
- No external PostgreSQL required (runs in container)

## Quick Start

### 1. Start Docker Desktop

Ensure Docker Desktop is running. Check with:
```bash
docker ps
```

If you get an error, start Docker Desktop from your Applications/Start Menu.

### 2. Start AsyncGate Stack

```bash
# From AsyncGate root directory
docker-compose up --build
```

One-command alternative:

```bash
./run_local.sh
# Windows PowerShell: .\run_local.ps1
```

This will:
- Build the AsyncGate Docker image
- Start PostgreSQL container
- Run database migrations
- Start AsyncGate API server

### 3. Verify It's Running

```bash
# Health check (MCP)
curl -s http://localhost:8000/mcp \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"asyncgate.health","arguments":{}}}'

# Should return: {"status":"healthy"}
```

### 4. Test with Command Executor Worker

**Terminal 1** (already running docker-compose):
```bash
docker-compose up
```

**Terminal 2** (start the worker):
```bash
cd workers/command_executor
pip install -r requirements.txt
python worker.py \
  --asyncgate-url http://localhost:8000 \
  --api-key dev-test-key-not-for-production
```

**Terminal 3** (queue a test task):
```bash
cd workers/command_executor
python test_command_executor.py \
  --asyncgate-url http://localhost:8000 \
  --api-key dev-test-key-not-for-production
```

### 5. Stop Everything

```bash
# Stop but keep data
docker-compose down

# Stop and remove data
docker-compose down -v
```

## Configuration

The `docker-compose.yml` uses these defaults:
- PostgreSQL: `localhost:5432` (from host)
- AsyncGate: `http://localhost:8000`
- API Key: `dev-test-key-not-for-production` (dev only!)
- Database: In Docker volume (persists between restarts)

## Viewing Logs

```bash
# All services
docker-compose logs -f

# Just AsyncGate
docker-compose logs -f asyncgate

# Just PostgreSQL
docker-compose logs -f postgres
```

## Rebuilding After Code Changes

```bash
# Rebuild and restart
docker-compose up --build

# Or rebuild without starting
docker-compose build
```

## Accessing the Database

```bash
# Connect to PostgreSQL
docker exec -it asyncgate-postgres psql -U asyncgate -d asyncgate

# Common queries
SELECT * FROM tasks LIMIT 10;
SELECT * FROM receipts ORDER BY created_at DESC LIMIT 10;
SELECT * FROM leases WHERE status = 'active';
```

## Common Issues

### Docker Desktop Not Running
```
Error: Cannot connect to Docker daemon
```
**Solution**: Start Docker Desktop from Applications/Start Menu

### Port Already in Use
```
Error: Port 8000 is already in use
```
**Solution**: Change port in docker-compose.yml or stop conflicting service

### Database Connection Failed
```
Error: Could not connect to PostgreSQL
```
**Solution**: 
- Check PostgreSQL container is healthy: `docker ps`
- View logs: `docker-compose logs postgres`
- Ensure no local PostgreSQL using port 5432

### Migrations Not Running
```
Error: Database schema out of date
```
**Solution**: Run migrations manually:
```bash
docker-compose exec asyncgate alembic upgrade head
```

## Development Workflow

1. **Make code changes** in `src/asyncgate/`
2. **Rebuild container**: `docker-compose up --build`
3. **Test changes** using worker + test script
4. **Check logs**: `docker-compose logs -f asyncgate`
5. **Iterate**

## Clean Slate Reset

If you need to start completely fresh:

```bash
# Stop everything
docker-compose down -v

# Remove images
docker rmi asyncgate

# Start fresh
docker-compose up --build
```

This removes all data, migrations, and images. You'll start with a clean database.

## Production Deployment

This docker-compose setup is **for development only**. For production:
- Use Fly.io deployment (see main README)
- Use managed PostgreSQL (not container)
- Change API keys and secrets
- Enable all security features
- Use docker-compose.prod.yml with proper configs

## Next Steps

Once AsyncGate is running locally:
1. Explore API endpoints at `http://localhost:8000/docs`
2. Create custom workers in `workers/`
3. Test different task types and receipt chains
4. Develop against local instance before deploying

Golden path demo (minimal worker loop):

```bash
python scripts/golden_path.py
```

## Support

If Docker setup fails:
1. Check Docker Desktop is running: `docker ps`
2. Check logs: `docker-compose logs`
3. Try clean slate reset (above)
4. Verify ports 8000 and 5432 are available
