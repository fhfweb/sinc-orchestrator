from services.semantic_resolver import SuffixIndex, SymbolTable, SymbolDefinition, ResolutionContext

def test_suffix_index():
    paths = ["services/auth.py", "models/user.py", "main.py"]
    idx = SuffixIndex(paths)
    
    assert "services/auth.py" in idx.resolve("services.auth")
    assert "services/auth.py" in idx.resolve("auth")
    assert "models/user.py" in idx.resolve("models/user")
    assert not idx.resolve("nonexistent")
    print("✓ SuffixIndex OK")

def test_tiered_resolution():
    st = SymbolTable()
    # Symbol in File A
    st.add(SymbolDefinition("User", "models/user.py", "Class", 1))
    # Symbol in File B
    st.add(SymbolDefinition("Auth", "services/auth.py", "Class", 1))
    # Same name in File C
    st.add(SymbolDefinition("User", "services/auth.py", "Class", 10))
    
    idx = SuffixIndex(["models/user.py", "services/auth.py"])
    ctx = ResolutionContext(st, idx)
    
    # 1. Same file priority
    res = ctx.resolve_symbol("User", "models/user.py")
    assert res[0].file_path == "models/user.py"
    
    # 2. Global fallback (no import yet)
    res = ctx.resolve_symbol("User", "main.py")
    assert len(res) == 2
    
    # 3. Import resolution
    ctx.add_import("services/auth.py", "models.user")
    res = ctx.resolve_symbol("User", "services/auth.py")
    # Should find 'User' in 'models/user.py' because it's imported (Tier 2),
    # but wait, the Tier 1 (Same File) has its own 'User' at line 10!
    assert res[0].file_path == "services/auth.py" # Tier 1 wins
    
    print("✓ Tiered Resolution OK")

if __name__ == "__main__":
    test_suffix_index()
    test_tiered_resolution()
    print("All tests passed!")
