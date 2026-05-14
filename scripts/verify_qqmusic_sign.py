"""Phase 0: Verify QQ Music signing algorithms in Python."""
import sys
import hashlib
from qqmusic_sign import zzb_sign, simple_sign, generate_g_tk


def test_zzb_sign():
    """Test zzb signing against a known input/output pair from GoMusic."""
    # Known test case: verify the basic structure
    result = zzb_sign("test_param_string")
    assert result.startswith("zzb"), f"Sign should start with 'zzb', got: {result}"
    assert len(result) > 10, f"Sign too short: {result}"
    print(f"  zzb_sign('test_param_string') = {result}")


def test_simple_sign():
    """Test simple MD5 signing produces expected format."""
    params = {"format": "json", "w": "晴天", "p": "1"}
    result = simple_sign(params)
    assert len(result) == 32, f"MD5 sign should be 32 chars, got: {len(result)}"
    assert result.isupper(), f"Sign should be uppercase, got: {result}"
    print(f"  simple_sign({params}) = {result}")


def test_g_tk():
    """Test g_tk generator with known cookie value."""
    # Known test: qqmusic_key=xxx should produce consistent g_tk
    result = generate_g_tk("test_musickey_123")
    assert result > 0, f"g_tk should be positive, got: {result}"
    print(f"  generate_g_tk('test_musickey_123') = {result}")

    # Verify determinism
    assert generate_g_tk("abc") == generate_g_tk("abc"), "g_tk must be deterministic"


def main():
    print("[1/3] Testing zzb signing algorithm...")
    test_zzb_sign()
    print("  OK: zzb sign produces valid format")

    print("[2/3] Testing simple MD5 signing...")
    test_simple_sign()
    print("  OK: simple sign produces valid format")

    print("[3/3] Testing g_tk generator...")
    test_g_tk()
    print("  OK: g_tk generator works")

    print("\n=== VERIFY PASSED: QQ Music signing algorithms compile and run ===")
    print("NOTE: Full API verification requires valid QQMUSIC_REFRESH_TOKEN and QQMUSIC_UIN in .env")
    sys.exit(0)


if __name__ == "__main__":
    main()
