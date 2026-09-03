class DomainError(Exception):
    """Base class for errors we want to surface as a clean HTTP response."""

    status_code = 400
    code = "domain_error"

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


class ValidationError(DomainError):
    status_code = 422
    code = "validation_error"


class JobNotFound(DomainError):
    status_code = 404
    code = "job_not_found"

    def __init__(self, job_id):
        super().__init__(f"Job {job_id} does not exist")


class IllegalTransition(DomainError):
    status_code = 409
    code = "illegal_transition"


class DuplicateJob(DomainError):
    status_code = 409
    code = "duplicate_job"
