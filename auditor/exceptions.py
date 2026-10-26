"""Domain exceptions. Messages are in Spanish because they are shown to end users."""


class AuditorError(Exception):
    """Base class for all expected, user-facing errors."""


class ConfigurationError(AuditorError):
    """Missing or invalid configuration (e.g. no API key)."""


class PDFExtractionError(AuditorError):
    """The PDF could not be opened or read."""


class AnalysisError(AuditorError):
    """The Claude API call failed or returned an unusable response."""
