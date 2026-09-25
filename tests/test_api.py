from fastapi.testclient import TestClient

from app.main import app


def test_health_and_openapi():
    with TestClient(app) as client:
        assert client.get('/health').status_code == 200
        schema=client.get('/openapi.json').json()
        assert '/api/v1/backtests' in schema['paths']
        settings = client.get('/api/v1/settings').json()
        serialized = str(settings).lower()
        assert 'api_secret' not in serialized and 'telegram_bot_token' not in serialized
        assert settings['trading_mode'] == 'paper' and not settings['live_trading_enabled']
