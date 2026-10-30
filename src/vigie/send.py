"""`vigie digest --send slack`: post this week's stored Digest, exactly as it was built."""

from dataclasses import dataclass

from vigie.digest import iso_week
from vigie.store import Store
from vigie.world import SlackError, World

# The `sent_to` of a Digest posted to Slack.
SLACK = "slack"


@dataclass
class SendReport:
    # ISO week, such as 2026-W44.
    week: str
    sent: bool = False
    # Why nothing was posted, when the Digest could not be sent.
    refused: str | None = None
    # The webhook's error: the Digest is not marked as sent.
    error: str | None = None


def send_digest(store: Store, world: World, *, force: bool = False) -> SendReport:
    """`vigie digest --send slack`: post the Slack payload stored with this week's Digest.

    Refuses when no Digest is stored for the week, or when it was already sent, unless `force`.
    """
    week = iso_week(world.now())
    digest = store.digest(week)
    if digest is None:
        return SendReport(
            week=week,
            refused=f"No Digest stored for {week}: run `vigie digest` first and review it.",
        )
    if digest.sent_at is not None and not force:
        return SendReport(
            week=week,
            refused=f"Digest for {week} already sent to {digest.sent_to}"
            f" on {digest.sent_at:%Y-%m-%d %H:%M %Z}.\nPass --force to send it again.",
        )
    if digest.slack_payload is None:
        return SendReport(
            week=week,
            refused=f"The Digest stored for {week} has no Slack payload yet:"
            " run `vigie digest --rebuild` and review it.",
        )
    try:
        world.post_slack(digest.slack_payload)
    except SlackError as e:
        return SendReport(week=week, error=str(e))
    store.mark_digest_sent(week, SLACK, world.now())
    return SendReport(week=week, sent=True)


def render_send_summary(report: SendReport) -> str:
    if report.refused:
        return report.refused
    if report.error:
        return (
            f"Could not send the Digest for {report.week}: {report.error}\n"
            "It is not marked as sent."
        )
    return f"Digest for {report.week} sent to Slack."
