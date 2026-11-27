from functools import lru_cache

from app.services.slack.sender import HttpSlackSender, SlackSender


@lru_cache
def get_slack_sender() -> SlackSender:
    return HttpSlackSender()
