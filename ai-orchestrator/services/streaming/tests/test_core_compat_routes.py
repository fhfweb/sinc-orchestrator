from services.streaming import create_app


def test_core_compat_routes_are_registered():
    app = create_app()
    routes = {(method, route.path) for route in app.routes for method in getattr(route, "methods", set())}

    assert ("POST", "/queue/poll") in routes
    assert ("POST", "/queue/release/{task_id}") in routes
    assert ("POST", "/tasks/claim") in routes
    assert ("POST", "/tasks/{task_id}/heartbeat") in routes
    assert ("POST", "/tasks/complete") in routes
    assert ("GET", "/scheduler/status") in routes
