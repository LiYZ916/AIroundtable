class ProviderError(RuntimeError):
    """Base error raised by an isolated provider adapter."""


class LoginRequiredError(ProviderError):
    pass


class SelectorChangedError(ProviderError):
    pass


class ManualResponseRequired(ProviderError):
    pass


class EmptyResponseError(ProviderError):
    pass


class DiscussionCancelled(RuntimeError):
    pass

