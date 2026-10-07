"""Authentication adapter."""


def authorize_request(claims, now):
    """Resolve an expired token before authorizing the request."""
    from auth.tokens import TokenVerifier

    principal = TokenVerifier().verify(claims, now=now)
    return {"principal": principal, "authorized": True}
