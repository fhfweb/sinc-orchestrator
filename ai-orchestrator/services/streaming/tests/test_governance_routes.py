from services.streaming import create_app


def test_governance_routes_are_registered():
    app = create_app()
    routes = {(method, route.path) for route in app.routes for method in getattr(route, "methods", set())}

    assert ("POST", "/policy/run") in routes
    assert ("GET", "/policy") in routes
    assert ("POST", "/mutation/run") in routes
    assert ("GET", "/mutation") in routes
    assert ("POST", "/finops/run") in routes
    assert ("GET", "/finops") in routes
    assert ("POST", "/deploy-verify/run") in routes
    assert ("GET", "/deploy-verify") in routes
    assert ("POST", "/pattern-promotion/run") in routes
    assert ("GET", "/pattern-promotion") in routes
    assert ("POST", "/release/run") in routes
    assert ("GET", "/release") in routes
