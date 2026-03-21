from service import DataService

def handle_request():
    svc = DataService()
    svc.save_data("Hello")
