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


def test_health_is_truthful_and_demo_only_paths_are_guarded():
    with TestClient(app) as client:
        health = client.get('/health').json()
        assert health['data_mode'] == 'REAL' and health['execution_mode'] == 'paper'
        # Nothing has been verified in the test process: no provider may claim to be healthy.
        for name in ('binance', 'gemini', 'finnhub', 'fred'):
            assert health['components'][name]['state'] in ('NOT_VERIFIED', 'NOT_CONFIGURED')
        assert health['status'] != 'HEALTHY'
        snapshot = {'symbol': 'BTCUSDT', 'price': 100, 'volume': 1500, 'volatility': .2, 'funding': 0,
                    'regime': 'trending', 'direction': 'BULLISH'}
        assert client.post('/api/v1/decision/evaluate', json=snapshot).status_code >= 400
        backfill = client.post('/api/v1/market-data/backfills', json={
            'exchange': 'paper', 'symbol': 'BTCUSDT', 'timeframe': '1h',
            'start': '2026-01-01T00:00:00Z', 'end': '2026-01-02T00:00:00Z'})
        assert backfill.status_code >= 400
        registry = client.get('/api/v1/system/jobs/registry').json()
        assert registry['items'] and all(item['purpose'] for item in registry['items'])


def test_seed_demo_refuses_outside_demo_mode():
    import pytest

    from app.core.config import Settings
    from app.seed import seed_demo

    with pytest.raises(RuntimeError, match='DEMO_MODE'):
        seed_demo(None, Settings(demo_mode=False))
