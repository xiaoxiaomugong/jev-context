"""Background job boundary."""


def run_job(operation):
    """Investigate retry exhaustion at this entry."""
    from worker.retry import execute_with_retry

    return execute_with_retry(operation)
