from worker.settings import MAX_ATTEMPTS


def execute_with_retry(operation):
    last_error = None
    for attempt in range(MAX_ATTEMPTS):
        try:
            return operation()
        except ConnectionError as error:
            last_error = error
    raise RuntimeError("RETRY_EXHAUSTED") from last_error
