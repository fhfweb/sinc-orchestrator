from services.streaming import create_app


def test_dashboard_debugger_routes_are_registered():
    app = create_app()
    routes = {
        (method, route.path)
        for route in app.routes
        for method in getattr(route, "methods", set())
    }

    assert ("GET", "/api/v5/dashboard/summary") in routes
    assert ("GET", "/api/v5/dashboard/task-debugger/{task_id}") in routes
    assert ("GET", "/dashboard") in routes
