
from logic import user_authentication
def run():
    if user_authentication("user", "pass"):
        print("Logged in")
