# AsyncGate Fly.io Operations

## Initial Setup
```bash
# Install Fly CLI
curl -L https://fly.io/install.sh | sh

# Login
fly auth login

# Deploy
./deploy-fly.sh
```

## Common Operations

### View logs
```bash
fly logs --app asyncgate
fly logs --app asyncgate -f  # Follow
```

### SSH into instance
```bash
fly ssh console --app asyncgate
```

### Scale instances
```bash
# Scale to 2 instances
fly scale count 2 --app asyncgate

# Scale to different regions
fly scale count 1 --region iad
fly scale count 1 --region lhr
```

### Update secrets
```bash
fly secrets set ASYNCGATE_API_KEY="new-key" --app asyncgate
fly secrets list --app asyncgate
```

### Database operations
```bash
# Connect to database
fly postgres connect -a asyncgate-db

# View database info
fly postgres db list -a asyncgate-db

# Create backup
fly postgres backup create -a asyncgate-db
```

### Monitoring
```bash
# View metrics
fly dashboard asyncgate

# Check health
curl -s https://asyncgate.fly.dev/mcp \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"asyncgate.health","arguments":{}}}'

# View all checks
fly checks list --app asyncgate
```

### Deployments
```bash
# Deploy latest
fly deploy --app asyncgate

# Deploy specific version
fly deploy --app asyncgate --image asyncgate:v0.3.0

# Rollback
fly releases --app asyncgate
fly releases rollback <version> --app asyncgate
```

### Troubleshooting
```bash
# Restart all instances
fly apps restart asyncgate

# View instance status
fly status --app asyncgate

# View VM list
fly vms list --app asyncgate

# Destroy and recreate (nuclear option)
fly apps destroy asyncgate
./deploy-fly.sh
```
