from services.streaming import create_app


def test_legacy_compat_routes_are_registered():
    app = create_app()
    paths = {route.path for route in app.routes}
    assert "/api/command" in paths
    assert "/api/config/confidence" in paths
    assert "/api/system/mode" in paths
    assert "/dashboard/state" in paths
