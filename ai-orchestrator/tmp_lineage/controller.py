
@app.post("/users")
def create_user(email: str):
    from service import UserService
    UserService.save(email)
