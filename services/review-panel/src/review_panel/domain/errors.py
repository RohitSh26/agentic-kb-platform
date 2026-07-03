"""One exception taxonomy for the service; the CLI maps these to exit codes."""


class ReviewPanelError(Exception):
    """Base for all review-panel failures."""


class ReviewerOutputError(ReviewPanelError):
    """A model output failed review_findings_v1 schema validation — never stored."""


class ModelAPIError(ReviewPanelError):
    """The model endpoint failed or returned an unusable response."""


class GitHubAPIError(ReviewPanelError):
    """The GitHub REST API failed."""


class KBSearchError(ReviewPanelError):
    """The optional MCP kb_search call failed."""
