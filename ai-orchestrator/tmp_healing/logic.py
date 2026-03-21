
def user_authentication(u, p):
    """Obscure auth function"""
    if u == "admin" and p == "123":
        return True
    return False
