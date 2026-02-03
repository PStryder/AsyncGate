"""
P0.3 + P0.4 Tests: CORS and Rate Limiting Configuration

Tests that CORS is properly configured and rate limiting is enabled.
"""

import pytest
from starlette.testclient import TestClient

from asyncgate.config import Environment, Settings
from asyncgate.main import app


def test_cors_no_wildcard_with_credentials():
    """
    Test that CORS is not configured with wildcard origins when credentials enabled.
    
    SECURITY: allow_origins=["*"] + allow_credentials=True = CSRF vulnerability
    """
    from asyncgate.config import settings
    
    # Verify credentials are enabled
    assert settings.cors_allow_credentials is True, "Credentials should be enabled"
    
    # Verify origins are NOT wildcard
    assert settings.cors_allowed_origins != ["*"], "Must not use wildcard with credentials"
    assert isinstance(settings.cors_allowed_origins, list), "Origins must be explicit list"
    assert len(settings.cors_allowed_origins) > 0, "Must have at least one allowed origin"
    
    print(f"✅ CORS configured safely: {settings.cors_allowed_origins}")


def test_cors_allows_localhost_in_dev():
    """Test that localhost is allowed for development."""
    from asyncgate.config import settings
    
    # Check default development origins
    localhost_origins = [
        origin for origin in settings.cors_allowed_origins 
        if "localhost" in origin or "127.0.0.1" in origin
    ]
    
    assert len(localhost_origins) > 0, "Localhost should be allowed for development"
    print(f"✅ Localhost origins configured: {localhost_origins}")


def test_cors_explicit_methods_and_headers():
    """Test that allowed methods and headers are explicitly configured."""
    from asyncgate.config import settings
    
    # Should not be wildcard
    assert settings.cors_allowed_methods != ["*"], "Methods should be explicit"
    assert settings.cors_allowed_headers != ["*"], "Headers should be explicit"
    
    # Should include essentials
    assert "GET" in settings.cors_allowed_methods
    assert "POST" in settings.cors_allowed_methods
    assert "Authorization" in settings.cors_allowed_headers
    assert "Content-Type" in settings.cors_allowed_headers
    
    print(f"✅ Explicit methods: {settings.cors_allowed_methods}")
    print(f"✅ Explicit headers: {settings.cors_allowed_headers}")


def test_rate_limiting_enabled_by_default():
    """
    Test that rate limiting is enabled by default.
    
    P0.4: Protects against DoS attacks and cost explosion.
    """
    from asyncgate.config import settings
    
    # Default should be True
    assert settings.rate_limit_enabled is True, "Rate limiting should be enabled by default"
    print(f"✅ Rate limiting enabled: {settings.rate_limit_enabled}")


def test_rate_limiting_forced_in_production():
    """Test that rate limiting is forced on in staging/production."""
    # Create production config
    prod_settings = Settings(
        env=Environment.PRODUCTION,
        rate_limit_enabled=False,  # Try to disable
        api_key="test-api-key",
        database_url="postgresql+asyncpg://test:test@localhost/test",
    )
    
    # Should be forced on despite config
    assert prod_settings.rate_limit_active is True, "Production must have rate limiting"
    
    # Create staging config
    staging_settings = Settings(
        env=Environment.STAGING,
        rate_limit_enabled=False,  # Try to disable
        api_key="test-api-key",
        database_url="postgresql+asyncpg://test:test@localhost/test",
    )
    
    assert staging_settings.rate_limit_active is True, "Staging must have rate limiting"
    
    # Development can disable
    dev_settings = Settings(
        env=Environment.DEVELOPMENT,
        rate_limit_enabled=False,
        api_key="test-api-key",
        database_url="postgresql+asyncpg://test:test@localhost/test",
    )
    
    assert dev_settings.rate_limit_active is False, "Development can disable rate limiting"
    
    print(f"✅ Rate limiting forced on in production/staging")


def test_rate_limiting_has_sane_defaults():
    """Test that rate limit defaults are reasonable."""
    from asyncgate.config import settings
    
    # Check defaults
    assert settings.rate_limit_default_calls == 100, "Default should be 100 calls"
    assert settings.rate_limit_default_window_seconds == 60, "Default window should be 60s"
    
    # Backend should be memory or redis
    assert settings.rate_limit_backend in ["memory", "redis"], "Backend must be memory or redis"
    
    print(f"✅ Rate limit defaults: {settings.rate_limit_default_calls} calls per {settings.rate_limit_default_window_seconds}s")


def test_cors_configuration_in_app():
    """Test that CORS middleware is applied to FastAPI app."""
    # Check middleware stack
    has_cors = False
    for middleware in app.user_middleware:
        if "CORSMiddleware" in str(middleware):
            has_cors = True
            break
    
    assert has_cors, "CORSMiddleware should be applied to app"
    print(f"✅ CORS middleware applied to app")


def test_cors_preflight_request():
    """Test CORS preflight OPTIONS request."""
    client = TestClient(app)
    
    # Preflight request
    response = client.options(
        "/mcp",
        headers={
            "Origin": "http://localhost:3000",
            "Access-Control-Request-Method": "POST",
        }
    )
    
    # Should allow the request
    assert response.status_code == 200
    assert "access-control-allow-origin" in response.headers
    
    print(f"✅ CORS preflight working: {response.headers.get('access-control-allow-origin')}")


def test_cors_rejects_unauthorized_origin():
    """Test that CORS rejects origins not in allowlist."""
    client = TestClient(app)
    
    # Request from unauthorized origin
    response = client.post(
        "/mcp",
        headers={"Origin": "https://evil.com"},
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "asyncgate.health", "arguments": {}},
        },
    )
    
    # Response should succeed (health check always works)
    # But CORS header should not allow evil.com
    cors_origin = response.headers.get("access-control-allow-origin")
    
    if cors_origin:
        assert cors_origin != "https://evil.com", "Should not allow unauthorized origin"
    
    print(f"✅ Unauthorized origin rejected")


if __name__ == "__main__":
    pytest.main([__file__, "-v", "-s"])
