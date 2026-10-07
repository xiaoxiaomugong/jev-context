from functools import wraps


def audited(function):
    @wraps(function)
    def wrapper(*args, **kwargs):
        return function(*args, **kwargs)
    return wrapper


class TokenVerifier:
    @audited
    def verify(
        self,
        claims: dict,
        *,
        now: int,
        leeway: int = 0,
    ) -> str:
        if claims["exp"] + leeway <= now:
            raise ValueError("AUTH_EXPIRED")
        return claims["sub"]
