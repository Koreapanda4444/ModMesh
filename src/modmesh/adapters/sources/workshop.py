from urllib.parse import urlencode

from modmesh.adapters.games.ark_evolved import WORKSHOP
from modmesh.core.sources import (
    SearchPage,
    SearchRequest,
    SourceAdapter,
    SourceCapability,
    SourceDescriptor,
)


class WorkshopSource(SourceAdapter):
    descriptor = SourceDescriptor(
        "workshop",
        "Steam Workshop",
        frozenset({"ark-evolved"}),
        frozenset({SourceCapability.SEARCH, SourceCapability.EXTERNAL_HANDOFF}),
    )

    def catalog_url(self, game_id: str) -> str | None:
        return WORKSHOP if game_id == "ark-evolved" else None

    async def search(self, request: SearchRequest) -> SearchPage:
        self.require(request.game_id, SourceCapability.SEARCH)
        url = WORKSHOP + "&" + urlencode({"searchtext": request.query})
        return SearchPage(
            (), warnings=("Subscribe and install through Steam Workshop",), handoff_url=url
        )
