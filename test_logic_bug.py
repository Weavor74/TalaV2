def add_numbers(a, b):
    # FIX: Changed subtraction to addition
    return a + b

if __name__ == "__main__":
    result = add_numbers(2, 3)
    print(f"Test executed. Result: {result}")
    try:
        assert result == 5
        print("SUCCESS: Assertion passed. logic bug fixed")
    except AssertionError:
        print("FAIL: Logic bug detected! Expected 5, got {}".format(result))
    print("Finished test run.")