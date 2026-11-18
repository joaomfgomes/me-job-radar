"""Job boards. SOURCES maps the name config.yaml uses for a board to the
class that reads it; adding a board means adding a class and one entry here."""

from jobradar.sources.linkedin import LinkedInGuestSource
from jobradar.sources.wellfound import WellfoundSource

SOURCES = {
    "linkedin": LinkedInGuestSource,
    "wellfound": WellfoundSource,
}
