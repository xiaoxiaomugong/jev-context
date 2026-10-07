from auth.entry import authorize_request


def test_authorize_request_rejects_boundary_expiration():
    try:
        authorize_request({"exp": 42, "sub": "sample"}, now=42)
    except ValueError as error:
        assert str(error) == "AUTH_EXPIRED"
    else:
        raise AssertionError("expired claims accepted")
