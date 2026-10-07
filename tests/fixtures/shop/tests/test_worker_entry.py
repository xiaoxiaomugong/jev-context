from worker.entry import run_job


def test_run_job_raises_after_three_attempts():
    calls = []

    def unavailable():
        calls.append(1)
        raise ConnectionError("offline")

    try:
        run_job(unavailable)
    except RuntimeError as error:
        assert str(error) == "RETRY_EXHAUSTED"
        assert len(calls) == 3
    else:
        raise AssertionError("failed operation returned")
